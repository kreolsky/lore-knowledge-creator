"""Durable per-object access counts — the object_access_daily rollup writer.

# ARCH: the never-purged layer beside telemetry_event. Raw events keep sequences
#       for a bounded window; this table keeps one counter per
#       (project_id, object_ref, day, actor, kind, user_id), because who opened,
#       read, saw-in-search or edited which object cannot be reconstructed later.
#       Producers: @track_agent_tool (agent) and POST /api/telemetry category
#       'access' (human). No reader yet — ranking use waits for data.
#
# WHY not in telemetry_store: that module's INVARIANT is append-only INSERT; an
# UPSERT counter is a different contract.
"""

from __future__ import annotations

from datetime import datetime, timezone

from db import get_db

# INVARIANT(corruption): never gather this query with other Surreal queries — surrealdb-py
# multiplexes one WS connection; concurrent queries contend. One FOR loop is one round-trip.
# Why: backend.md "no gather on shared Surreal conn".
_UPSERT = """
FOR $r IN $rows {
    UPSERT type::record('object_access_daily',
        [$r.project_id, $r.object_ref, $day, $r.actor, $r.kind, $r.user_id])
    SET count += 1, project_id = $r.project_id, object_ref = $r.object_ref,
        day = $day, actor = $r.actor, kind = $r.kind, user_id = $r.user_id;
};
"""


def doc_ref(document_id: str) -> str:
    """object_ref of a document or reference (both live in `documents`)."""
    return f"doc:{document_id}"


async def count_access(rows: list[dict]) -> None:
    """Add 1 to today's (UTC) counter for each row, in ONE query.

    Each row: project_id, object_ref, actor ('human'|'agent'),
    kind ('open'|'read'|'exposure'|'edit'), user_id. Raises on DB failure —
    callers are telemetry paths and decide how to swallow.
    """
    if not rows:
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = await get_db()
    await db.query(_UPSERT, {"rows": rows, "day": day}, site="access_counts")
