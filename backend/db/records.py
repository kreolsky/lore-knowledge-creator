"""Record serialization + record-id coercion helpers (pure, no DB connection).

Split out of the former backend/db.py (behavior-preserving).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def extract_id(record_id) -> str | None:
    """Extract plain UUID string from a SurrealDB RecordID, string, or None.

    surrealdb SDK 1.0.4 returns RecordID objects serialised as "table:id"
    where id may be wrapped in backticks or angle brackets.
    """
    if record_id is None:
        return None
    s = str(record_id)
    # Format: "RecordID(table_name=x, record_id=`uuid`)" OR "x:`uuid`" OR "x:uuid"
    if "record_id=" in s:
        # SDK repr: RecordID(table_name=users, record_id=`uuid`)
        part = s.split("record_id=", 1)[1].rstrip(")")
        return part.strip("⟨⟩`")
    if ":" in s:
        raw = s.split(":", 1)[1]
        return raw.strip("⟨⟩`")
    return s.strip("⟨⟩`")


def is_record_id(value) -> bool:
    """Return True if `value` is a SurrealDB RecordID instance.

    # ARCH (version-robust parity with serialize_record): the RecordID class is
    # detected by NAME, not by importing the SDK symbol. Why: the symbol's module
    # path drifts across SDK versions (see extract_id's 1.0.4 note), so a hard
    # import would break on a bump. The name-based duck-type (`"RecordID" in
    # type(v).__name__`) matches every version's RecordID class and nothing else,
    # so this single detector is shared by serialize_record (inbound, per-row) and
    # the MCP gateway's output boundary (coerce_record_ids) — two copies can no
    # longer drift.
    """
    return "RecordID" in type(value).__name__


def coerce_record_ids(obj: Any) -> Any:
    """Recursively normalize SurrealDB RecordID instances to plain uuid strings.

    Walks dicts/lists and coerces every RecordID found anywhere in the structure
    via extract_id; plain values pass through unchanged (extract_id is idempotent
    on already-plain values). This is the single record-id coercion at the MCP
    gateway's output boundary (`server._ok`), moved here so the inbound serializer
    (serialize_record) and the outbound gateway share ONE walk.

    # ARCH (supersedes plan "mcp-gateway-debt-paydown" Decision 6): the
    # normalization is RECURSIVE and TYPE-BASED — it coerces any SurrealDB
    # `RecordID` instance found anywhere in the result (dict values, list items,
    # nested), via db.extract_id. Why type-based not key-name-based: the previous
    # top-level `_ID_KEYS` match missed a RecordID leaking through a nested
    # list/dict (e.g. a row echoed inside `documents: [...]`), and a key-name
    # match risks mangling a legitimate string that merely shares an id key name.
    # Coercing ONLY genuine RecordID instances is precise (plain strings/ints are
    # never touched) and closes the nested leak at the single output boundary —
    # per-boundary, not per-emitter, so a future nested emitter is covered too.
    # extract_id is idempotent on already-plain values.
    """
    if is_record_id(obj):
        return extract_id(obj)
    if isinstance(obj, dict):
        return {k: coerce_record_ids(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [coerce_record_ids(v) for v in obj]
    return obj


def _fmt_datetime(v) -> str | None:
    """Format a datetime value to 'YYYY-MM-DD HH:MM' for client display."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return None
    return None


def serialize_record(record: dict, id_field: str) -> dict:
    """Convert a SurrealDB record dict to a client-compatible dict.

    Adds '*_fmt' formatted strings for all datetime fields ending in '_at'
    (except deleted_at) so the frontend doesn't need to format dates.
    """
    result: dict = {}
    for k, v in record.items():
        # INVARIANT(corruption): ydoc_state is an internal CRDT snapshot (raw bytes) — it must
        # never reach an API response: JSON serialization can't UTF-8-decode it,
        # and clients have no use for it.  Why: ydoc_state is raw CRDT bytes the client can't decode or use; leaking it would ship a multi-KB binary that breaks JSON serialization, so it's stripped at the single outbound seam. Stripped here, the single outbound
        # boundary. Why: SELECT * (fetch_one) pulls it in; see ydoc_store.
        if k == "ydoc_state":
            continue
        if k == "id":
            result[id_field] = extract_id(v)
        elif is_record_id(v):
            result[k] = extract_id(v)
        elif isinstance(v, datetime):
            # INVARIANT(persisted): datetime columns are emitted as ISO-8601 strings, never raw
            # datetime objects. Why: serialize_record feeds event-bus payloads that pass
            # through json.dumps (CollabSession.broadcast); SurrealDB 2.0+ deserializes
            # datetime columns to Python datetime, which json.dumps cannot encode — the
            # WS frame would silently fail inside the event-bus subscriber.
            result[k] = v.isoformat()
        else:
            result[k] = v
        if k.endswith("_at") and k != "deleted_at":
            result[f"{k}_fmt"] = _fmt_datetime(v)
    return result
