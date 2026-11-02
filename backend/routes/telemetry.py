"""Telemetry ingest — append-only diagnostics from live clients.

# SYSTEM: telemetry — POST /api/telemetry ingest for the queryable telemetry_event table.
# ARCH: thin ingest. Validates/clamps, then defers the write to telemetry_store.

INVARIANT: append-only diagnostics — never mutates app state, and tolerates
malformed/oversized input without 500s (drop/clamp, return {accepted: n}).
Why: telemetry is fire-and-forget from the browser; a bad batch must never surface
as an error to the user or break the page's flush-on-unload.
"""

from __future__ import annotations

import logging
from datetime import datetime

from access_counts import count_access, doc_ref
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from access import get_doc_project_id, get_project_access
from auth import get_current_user
from telemetry_store import clamp_detail, record_telemetry_events

router = APIRouter()
logger = logging.getLogger("lore.collab.client")

# WHY: bound the batch and the per-event detail so the endpoint can't be abused
# into an unbounded write. Why: the body is attacker-controllable; excess is dropped,
# never errored.
MAX_BATCH = 50

# kinds that warrant an at-a-glance WARNING in lore.collab.client (prod-debug signal)
_WARN_COLLAB_KINDS = {"close", "gave-up", "server-reap", "edit-dead-doc", "edit-unsent"}
_WARN_PERF_KINDS = {"lag", "history-large"}


class TelemetryEvent(BaseModel):
    category: str
    kind: str
    project_id: str | None = None
    entity_id: str | None = None
    detail: dict = Field(default_factory=dict)
    client_ts: datetime | None = None


class TelemetryBatch(BaseModel):
    events: list[TelemetryEvent]


def _should_warn(ev: TelemetryEvent) -> bool:
    if ev.category == "collab":
        return ev.kind in _WARN_COLLAB_KINDS
    if ev.category == "perf":
        return ev.kind in _WARN_PERF_KINDS
    return False


@router.post("/api/telemetry")
async def ingest_telemetry(body: TelemetryBatch, user: dict = Depends(get_current_user)):
    """Accept a bounded batch of client telemetry events; insert them append-only."""
    uid = user["user_id"]
    events = body.events[:MAX_BATCH]  # truncate, don't reject — keep the head
    rows: list[dict] = []
    for ev in events:
        row = {
            "category": ev.category,
            "kind": ev.kind,
            "user_id": uid,
            "project_id": ev.project_id,
            "entity_id": ev.entity_id,
            "detail": clamp_detail(ev.detail),
            "client_ts": ev.client_ts,
        }
        rows.append(row)
        if _should_warn(ev):
            logger.warning(
                "telemetry %s/%s user=%s entity=%s detail=%s",
                ev.category, ev.kind, uid, ev.entity_id, row["detail"],
            )
        else:
            logger.info("telemetry %s/%s user=%s", ev.category, ev.kind, uid)

    try:
        accepted = await record_telemetry_events(rows)
    except Exception:
        # One acceptable swallow: diagnostics ingest must never 500 a client.
        logger.warning("telemetry insert failed (dropped %d rows)", len(rows), exc_info=True)
        return {"accepted": 0}
    try:
        await count_access(await _human_access_rows(events, user))
    except Exception:
        logger.warning("access rollup failed", exc_info=True)
    return {"accepted": accepted}


async def _human_access_rows(events: list[TelemetryEvent], user: dict) -> list[dict]:
    """object_access_daily increments for `access/open` events on readable documents.

    INVARIANT(security): an open is counted only for a document the caller can read,
    and under the document's OWN project, never the client-sent project_id. Why: the
    body is attacker-controllable; otherwise any member could write counts for ids
    outside their reach. Document access IS project access (see get_document_access),
    so the project lookup doubles as the check. Serial awaits — no gather on the conn.
    """
    rows: list[dict] = []
    for ev in events:
        if ev.category != "access" or ev.kind != "open" or not ev.entity_id:
            continue
        project_id = await get_doc_project_id(ev.entity_id)
        if project_id is None or not await get_project_access(project_id, user):
            continue
        rows.append({
            "project_id": project_id, "object_ref": doc_ref(ev.entity_id),
            "actor": "human", "kind": "open", "user_id": user["user_id"],
        })
    return rows
