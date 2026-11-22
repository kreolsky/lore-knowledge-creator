"""Telemetry retention task — the per-category purge behind the telemetry INVARIANT."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# INVARIANT: telemetry rows are purged after 90 days, agent_tool and access rows after 180.
# Why: telemetry_event is append-only diagnostics ("логов мало не бывает"); volume
# is bounded by retention + indexing, not by dropping data at write time. 90d covers
# collab/perf reconnect patterns (and the join of telemetry_event.detail.call_id ↔
# the driver log's tool-call ids across turns/retries). agent_tool rows are the
# only record of how the agent navigates the knowledge base (read/search sequences
# that cannot be reconstructed later), so they keep two quarters of usage history;
# access rows (human opens) are the same navigation record for people.
# Raise the perf aggregate-window size before lowering this — keep the signal,
# shrink the granularity.
TELEMETRY_RETENTION_DAYS = 90
AGENT_TOOL_RETENTION_DAYS = 180
TOKEN_USAGE_RETENTION_DAYS = 365
PIPELINE_RUNS_RETENTION_DAYS = 90


async def telemetry_retention_task(ctx) -> None:
    """Delete telemetry_event rows older than the retention window.

    Single bounded DELETE, serial (no gather on the shared Surreal conn — backend.md).
    Idempotent and cheap to re-run daily.
    """
    from db import get_db

    db = await get_db()
    await db.query(
        f"DELETE telemetry_event WHERE category NOT IN ['agent_tool', 'access'] "
        f"AND created_at < time::now() - {TELEMETRY_RETENTION_DAYS}d",
        site="telemetry_retention",
    )
    await db.query(
        f"DELETE telemetry_event WHERE category IN ['agent_tool', 'access'] "
        f"AND created_at < time::now() - {AGENT_TOOL_RETENTION_DAYS}d",
        site="telemetry_retention",
    )
    # INVARIANT(data-loss): token_usage rows are purged after 365 days.
    # Why: operator ruling — one year of spend history.
    await db.query(
        f"DELETE token_usage WHERE at < time::now() - {TOKEN_USAGE_RETENTION_DAYS}d",
        site="telemetry_retention",
    )
    # INVARIANT(data-loss): pipeline_runs rows are purged after 90 days.
    # Why: operator ruling — the run log is
    # operational history ("what did this binding do lately"), not a record of
    # knowledge; 90 days matches telemetry_event because a run's debugging value
    # (correlating a bad extraction with its run) decays on the same clock. The
    # OUTPUT documents are the durable artifact and are not touched.
    await db.query(
        f"DELETE pipeline_runs WHERE queued_at < time::now() - {PIPELINE_RUNS_RETENTION_DAYS}d",
        site="telemetry_retention",
    )
    logger.info(
        "telemetry retention: purged rows older than %dd (agent_tool/access: %dd)",
        TELEMETRY_RETENTION_DAYS, AGENT_TOOL_RETENTION_DAYS,
    )
