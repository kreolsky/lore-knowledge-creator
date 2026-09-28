"""Telemetry retention task — the 90d purge behind the telemetry INVARIANT."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# INVARIANT: telemetry rows are purged after 90 days. Why: telemetry_event is
# append-only diagnostics ("логов мало не бывает"); volume is bounded by retention +
# indexing, not by dropping data at write time. 90d covers collab/perf reconnect
# patterns AND agent-tool usage (enough horizon to tune tool descriptions from
# real usage data, and to join telemetry_event.detail.call_id
# ↔ the driver log's tool-call ids across turns/retries). Applies to ALL
# categories (collab|perf|agent|agent_tool). Raise the perf aggregate-window size
# before lowering this — keep the signal, shrink the granularity.
TELEMETRY_RETENTION_DAYS = 90


async def telemetry_retention_task(ctx) -> None:
    """Delete telemetry_event rows older than the retention window.

    Single bounded DELETE, serial (no gather on the shared Surreal conn — backend.md).
    Idempotent and cheap to re-run daily.
    """
    from db import get_db

    db = await get_db()
    await db.query(
        f"DELETE telemetry_event WHERE created_at < time::now() - {TELEMETRY_RETENTION_DAYS}d",
        site="telemetry_retention",
    )
    logger.info("telemetry retention: purged rows older than %dd", TELEMETRY_RETENTION_DAYS)
