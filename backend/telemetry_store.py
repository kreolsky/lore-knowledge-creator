"""Shared insert path for telemetry_event rows.

# SYSTEM: telemetry — append-only diagnostics channel (collab reconnects, perf/lag).
# ARCH: one batched INSERT per call (single round-trip). Both the REST route
#       (routes/telemetry.py) and the backend correlation producer (collab session
#       reap) write through here so the wire shape never drifts.

INVARIANT: append-only — this module only ever INSERTs diagnostics rows; it never
mutates application state. Why: telemetry must be safe to drop/clamp and must never
be able to corrupt or 500 a real request path.
"""

from __future__ import annotations

import json
import logging

from db import get_db

logger = logging.getLogger(__name__)

# INVARIANT(corruption): never gather these INSERTs with other Surreal queries — surrealdb-py
# multiplexes one WS connection; concurrent queries contend. A single batched INSERT
# is one round-trip. Why: backend.md "no gather on shared Surreal conn".

# Per-event detail cap shared by EVERY producer (REST ingest, collab reap, the
# @track_agent_tool decorator). Lives here — the leaf imported by all of them — so the
# cap + the truncate sentinel never drift between producers writing the same column.
MAX_DETAIL_BYTES = 4096

# The largest a single scalar value may be and still be PRESERVED when the blob is
# truncated. Short ids (a 36-char run_id, a reference id, a slug) sit well under this;
# a free-form string (content, old_string, a verdict's text) does not — so the
# diagnosis field survives while the noise is dropped. Sized so a couple of dozen short
# scalars still fit the MAX_DETAIL_BYTES cap when kept together.
_MAX_KEEP_SCALAR_BYTES = 256


def _truncate_keep_scalars(detail: dict) -> dict:
    """The truncation outcome for an over-cap dict: keep SHORT SCALAR fields, drop the
    rest, mark the row truncated.

    A verdict batch (or a large edit body) always exceeds the cap, but the SHORT ids
    alongside it (run_id, reference_id, target_doc_id) are the fields a later diagnosis
    reads — so they survive while only the large / non-scalar values are dropped. The
    result is bounded by MAX_DETAIL_BYTES and always carries `_truncated: True` so a
    consumer never mistakes it for the full blob (clamp_detail previously returned
    `{"_truncated": True}` for every oversized batch, dropping the one field — a 36-char
    run_id — that held the answer and making the next occurrence undiagnosable).
    """
    kept: dict = {"_truncated": True}
    # Running byte budget over the serialized form, starting from the marker's footprint.
    budget = MAX_DETAIL_BYTES - len(json.dumps({"_truncated": True}))
    for key, value in detail.items():
        if not isinstance(key, str):
            continue
        # Keep scalars only (str / int / float / bool / None); lists + dicts are the
        # structured noise that blew the cap (a verdicts batch, an edits list).
        if isinstance(value, str):
            ser = json.dumps(value, default=str)
        elif isinstance(value, (int, float, bool)) or value is None:
            ser = json.dumps(value, default=str)
        else:
            continue
        if len(ser) > _MAX_KEEP_SCALAR_BYTES:
            continue  # a scalar but not SHORT (a long string) → drop
        # Marginal serialized cost of `"key":value,` inside the kept object.
        cost = len(key) + len(ser) + 4
        if cost > budget:
            continue
        kept[key] = value
        budget -= cost
    return kept


def clamp_detail(detail: dict) -> dict:
    """Clamp the detail blob to the cap, preserving SHORT SCALAR fields under truncation.

    Shared by all telemetry producers so the clamp is identical everywhere. Has a
    cheap early-exit: a single string value already larger than the cap guarantees the
    serialized form exceeds it, so we skip the full serialize WITHOUT truncating the
    whole blob to the bare marker — the short scalars beside it still survive. When the
    blob IS over cap, only the large / non-scalar values are dropped (`_truncate_keep_
    scalars`), so a 36-char run_id that holds a later diagnosis survives even when a
    sibling verdict batch blows the cap.
    """
    if isinstance(detail, dict):
        if not any(
            isinstance(v, str) and len(v) > MAX_DETAIL_BYTES for v in detail.values()
        ):
            try:
                if len(json.dumps(detail, default=str)) <= MAX_DETAIL_BYTES:
                    return detail
            except (TypeError, ValueError):
                pass  # WHY: non-serializable → fall through to scalar-preserving truncate.
        return _truncate_keep_scalars(detail)
    try:
        if len(json.dumps(detail, default=str)) <= MAX_DETAIL_BYTES:
            return detail
    except (TypeError, ValueError):
        pass  # WHY: detail too large or non-serializable → drop the payload, keep the row.
    return {"_truncated": True}


async def record_telemetry_events(rows: list[dict]) -> int:
    """Insert telemetry rows in ONE batched INSERT. Returns the count attempted.

    Each row is a dict with: category, kind, user_id, optional project_id/entity_id/
    client_ts, and a `detail` object. created_at is filled by the schema DEFAULT.
    Surfaces SurrealError (surrealdb 2.0 strict errors) — callers decide whether to
    swallow; the REST route swallows (diagnostics must never 500), the reap producer
    logs-and-continues.
    """
    if not rows:
        return 0
    db = await get_db()
    await db.query("INSERT INTO telemetry_event $rows", {"rows": rows}, site="telemetry_insert")
    return len(rows)
