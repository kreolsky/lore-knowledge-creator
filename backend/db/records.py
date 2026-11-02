"""Record serialization + record-id coercion helpers (pure, no DB connection).

Split out of the former backend/db.py (behavior-preserving). Record-id
validation (validate_record_id / SAFE_ID_RE) lives here, not in db._patch, so
_patch holds only the SDK monkeypatches and is deletable on an SDK upgrade.
"""

from __future__ import annotations

import re
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


# SECURITY: Validate record IDs before embedding in RELATE queries.
# SurrealDB RELATE doesn't support parameterized record IDs, so we
# must ensure IDs contain only safe characters (UUID format).
# M-4: ASCII-only to prevent Unicode homoglyph injection
SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9_\-]+$')


def validate_record_id(rid: str) -> str:
    """Raise ValueError if rid contains unsafe characters for SurrealDB queries."""
    if not SAFE_ID_RE.match(rid):
        raise ValueError(f"Invalid record ID: {rid!r}")
    return rid


def record_refs(table: str, ids: list[str], prefix: str = "id") -> tuple[str, dict]:
    """One param-bound `type::record()` IN list: returns (fragment, params).

    SurrealDB matches a record row only through `type::record(table, $id)` — a
    bare `id IN $ids` with string ids matches nothing (the id column is a
    record ref), and a list-arg `type::record(table, $ids)` doesn't bulk-match
    either; only per-id record refs work. This is the single builder for that
    idiom. The table and every id pass validate_record_id, and the ids ride
    query params — id bytes never reach query text.

    Why per-id refs and not the `$rids.map(...)` binding the record-targeted
    UPDATEs use: a `WHERE id IN [...]` UPDATE scans the whole table and its
    read set conflicts with any concurrent write to it, so writes target
    records directly (`UPDATE $rids.map(...) SET ...`, e.g. delete.py's
    tombstone); a SELECT's IN-clause takes no conflicting write locks, and
    the per-id form keeps every id a bound param here.
    """
    validate_record_id(table)
    params: dict[str, str] = {}
    refs: list[str] = []
    for n, rid in enumerate(ids):
        validate_record_id(rid)
        params[f"{prefix}{n}"] = rid
        refs.append(f"type::record('{table}', ${prefix}{n})")
    return ", ".join(refs), params


def build_set_clause(updates: dict, *, none_as_NONE: bool = False) -> str:
    """One param-bound SET clause from an updates dict: `k = $k` parts, ", "-joined.

    Single home of the per-route set_parts/assignments builders.
    Keys are always code-chosen column names (Pydantic fields or hardcoded
    literals), never user input — the same injection posture every copy had.
    Divergence the copies carried, named: with none_as_NONE a None VALUE writes
    `k = NONE` (unbinds the column) and the caller binds only its non-None
    params; without it every value rides a $k param. Time columns
    (`updated_at = time::now()`) and revivor suffixes stay at the callers —
    they are SQL the caller appends, not part of the mapping.
    """
    parts: list[str] = []
    for k, v in updates.items():
        if none_as_NONE and v is None:
            parts.append(f"{k} = NONE")
        else:
            parts.append(f"{k} = ${k}")
    return ", ".join(parts)


def iso_str(
    value: object,
    *,
    default: str | None = None,
    passthrough_str: bool = False,
    date_only: bool = False,
) -> str | None:
    """Serialize a stored datetime cell to an ISO string for JSON transport.

    Why single-homed: SurrealDB 2.0 returns Python `datetime` and raw values
    fail json.dumps, so every surface grew the same local `_iso`.
    The divergences between surfaces are NAMED parameters, never silently
    picked:
      default — what a non-datetime, non-passthrough cell yields ("" for the
        digest/branch shapes, None for the optional-column shapes)
      passthrough_str — a stored string passes through instead of hitting
        `default` (public_share/readonly_executors/recall_search kept it)
      date_only — date granularity (digest material lines) instead of the
        full datetime
    Surreal cells are datetime | str | None, so any other scalar yields
    `default` like every other non-datetime cell.
    """
    if isinstance(value, datetime):
        return value.date().isoformat() if date_only else value.isoformat()
    if passthrough_str and isinstance(value, str):
        return value
    return default


def parse_iso(value: str) -> datetime:
    """Parse a stored field's ISO string to a datetime (Z suffix tolerated).

    Single home of the `datetime.fromisoformat(v.replace("Z", "+00:00"))` arm
    in the stored-cell coercions (doc_proximity._ts, jobs thinning,
    api_key_auth expiry, completions_turn._parse_utc_datetime).
    Python 3.11+ parses a trailing Z natively, so the replace is
    belt-and-braces — same result either way. Callers own the
    coercion shape around it (datetime passthrough, naive→UTC, fallbacks):
    this is the parse primitive only.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
