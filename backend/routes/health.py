"""Health check endpoint — reports service status, DB/Redis/agent-driver connectivity."""

import asyncio
import time

import httpx
from backplane import get_backplane
from code_stamp import CODE_STAMP, WORKER_STAMP_REDIS_KEY
from collab.flush_pipeline import flush_pipeline_failures
from collab.registry import collab_registry_failures
from collab.session import collab_session_failures
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from query_stats import snapshot as query_stats_snapshot
from surrealdb import AsyncSurreal

import config
from auth import get_current_user
from db import get_db
from event_bus import (
    backplane_publish_failures,
    backplane_subscription_failures,
    subscriber_failures,
)
from ydoc_store import durability_degraded_count

router = APIRouter()

# Redis is a hard dependency (see backplane.py: Redis down = service down), so every
# /api/health read pays ONE Redis round-trip (_probe_redis — liveness and the worker
# stamp are the same GET). Bounded: Backplane.get has no internal timeout, and an
# unbounded await would let a WEDGED (not refused) Redis connection hang the endpoint
# a load balancer scrapes — the exact failure the probe exists to report.
_REDIS_PROBE_TIMEOUT_S = 1.5
# Driver probe bound (the health report's OWN probe — the capability signal
# probes nothing). Deliberately tight: /api/health is a target a load
# balancer scrapes, so a slow-but-alive driver must not stall the scrape.
_DRIVER_PROBE_TIMEOUT_S = httpx.Timeout(2.0, connect=1.0)

# TTL-memoized storage metric. /api/health is scraped frequently by monitoring /
# load balancers, and `cp_blobs` is a full-table aggregate scan (immortal blobs → the
# table grows unboundedly). This cache decouples scrape cadence from scan cost so a
# high-frequency scraper cannot turn the endpoint into a per-request full scan.
# Plain process-local memo (per-replica); concurrent expiry recomputes once — harmless.
_STORAGE_CACHE_TTL_SEC = 60
_storage_cache: dict | None = None
_storage_cache_at: float = 0.0


async def _probe_redis() -> tuple[bool, str | None]:
    """One bounded Redis GET answering BOTH health questions: liveness, and the
    worker's published code stamp.

    Returns (alive, stamp). A successful read IS the liveness signal, so this is
    one round-trip, not two — the stamp key is the probe key. A None stamp with
    alive=True means no worker has booted yet; alive=False means the read raised
    or timed out. Dead Redis must read 503 here, not indirectly through
    backplane_publish_failures counters that stay at zero on an idle service
    because nobody tried to publish.

    Bounded: Backplane.get has no internal timeout, and an unbounded await would
    let a WEDGED (not refused) Redis connection hang the endpoint a load balancer
    scrapes — the exact failure the probe exists to report.
    """
    try:
        raw = await asyncio.wait_for(
            get_backplane().get(WORKER_STAMP_REDIS_KEY), timeout=_REDIS_PROBE_TIMEOUT_S
        )
    except Exception:
        return False, None
    if raw is None:
        return True, None
    return True, raw.decode() if isinstance(raw, bytes) else str(raw)


async def _probe_agent_line(line) -> str:
    """One driver line's liveness: "reachable" | "unreachable" |
    "secret_mismatch".

    # WHY: the probe result only DEGRADES the report (200 + named in the
    # body) — never a 503 — because the editor and collab work fine without
    # the agent line; pulling the service out of rotation over a chat outage
    # would invert the dependency graph. Unconfigured lines are skipped by the
    # caller: an unset optional line is a choice, not an outage.
    # A 401 is its OWN name: the service is up and refusing — the backend and
    # the harness hold different driver secrets, and the report says so
    # instead of crying "unreachable" (plan component-wiring-not-settings).
    """
    try:
        async with httpx.AsyncClient(timeout=_DRIVER_PROBE_TIMEOUT_S) as client:
            resp = await client.get(
                f"{line.url}/health",
                headers={"X-Driver-Secret": line.secret},
            )
        if resp.status_code == 401:
            return "secret_mismatch"
        if resp.status_code == 200 and resp.json().get("status") == "ok":
            return "reachable"
        return "unreachable"
    except Exception:
        return "unreachable"


async def _probe_agent_lines() -> dict[str, str]:
    """THE agent line's liveness: {name: reachable | unreachable |
    secret_mismatch | unconfigured}. Unconfigured answers "unconfigured"
    without a probe (the resolver INVARIANT) — a line that was never enabled
    is not an outage. Kept a one-entry map for the `agent_lines` body shape."""
    from driver.client import DRIVER_LINE_NAME, resolve_driver_line

    line = await resolve_driver_line()
    return {DRIVER_LINE_NAME: "unconfigured" if line is None else await _probe_agent_line(line)}


def _stability_counters() -> dict:
    """Best-effort-guard tripwires: per-replica in-process counters for the collab
    cluster, the append-log durability path, and event_bus guards."""
    return {
        # Surfaces the silent Redis SPOF: a climbing count means cross-process events
        # are being lost (Redis down / flaky) while the service otherwise looks healthy.
        "backplane_publish_failures": backplane_publish_failures(),
        # Append-log durability failures: a non-zero/climbing count means Yjs edits reached
        # live peers but failed to persist to the append-only log → those edits vanish on
        # restart (silent data loss). Mirrors backplane_publish_failures. See ydoc_store.
        # NOTE: this is a PER-REPLICA in-process counter (process start → now), NOT a global
        # aggregate — each web replica reports only its own failures. A multi-replica alert
        # must scrape every replica's /api/health; summing them is the caller's job. (A
        # shared Redis counter for global aggregation is a separate, unimplemented concern.)
        "durability_degraded_count": durability_degraded_count(),
        # Plan 1.1 — collab catch-all cluster tripwires. Every best-effort guard in the
        # collab cluster (flush pipeline sub-ops, session guards, registry teardown)
        # increments its module counter; a climbing value means a persistent failure
        # that only logs is happening. Per-replica in-process counters (see the
        # durability_degraded_count NOTE above).
        # OBSERVABILITY: the flush_to_db failure itself is NOT in these counters — it is a
        # discrete incident, query telemetry `kind='flush_failure'` (docs/telemetry.md
        # "Collab observability"). An alert on flushes must poll telemetry, not here.
        "collab_failure_counters": {
            "flush_pipeline": flush_pipeline_failures(),
            "session": collab_session_failures(),
            "registry": collab_registry_failures(),
        },
        # event_bus best-effort guards (siblings of backplane_publish_failures): one bad
        # subscriber must not break the bus, and a skipped backplane subscription means
        # cross-replica events for that type are lost — both counted, not just logged.
        "event_bus_subscriber_failures": subscriber_failures(),
        "event_bus_subscription_failures": backplane_subscription_failures(),
    }


def _health_body(
    db_ok: bool, worker_stamp: str | None, redis_ok: bool,
    agent_lines: dict[str, str] | None = None,
) -> dict:
    """Assemble the /api/health response payload (observability counters + drift)."""
    # A configured line being unreachable — or refusing with a secret
    # mismatch — degrades; an UNCONFIGURED line never does (it was never
    # enabled — reporting it as degraded would cry wolf on a driver-less
    # deployment).
    lines_down = any(
        v in ("unreachable", "secret_mismatch") for v in (agent_lines or {}).values()
    )
    status = (
        "ok"
        if db_ok and redis_ok and not lines_down
        else "degraded"
    )
    return {
        "status": status,
        "db": "connected" if db_ok else "disconnected",
        # Redis is a hard dependency (backplane's hard rule: Redis down = service
        # down) — its loss is reported directly here, not only via the
        # publish-failure counters.
        "redis": "connected" if redis_ok else "disconnected",
        # The agent line BY NAME (a one-entry map for the body shape).
        # Optional dependency: degraded-status-only (see _probe_agent_line).
        "agent_lines": agent_lines or {},
        # Release tag of the running build (see the `release` skill). Read through the
        # module, not a from-import, so the value stays patchable per call.
        # Deliberately unauthenticated: the user cabinet reads it from here, and it is
        # the one field a bug report can always quote.
        "version": config.APP_VERSION,
        # Worker code-version drift: the worker runs from a separately-built image, so a
        # `--build backend`-only deploy leaves it on stale jobs/pipeline code. worker_stale
        # surfaces that mismatch; null worker_code_stamp = no worker has booted yet.
        "code_stamp": CODE_STAMP,
        "worker_code_stamp": worker_stamp,
        "worker_stale": worker_stamp is not None and worker_stamp != CODE_STAMP,
        **_stability_counters(),
        # Per-site SurrealDB query latency (count / total_ms / max_ms). Surfaces shared-
        # connection contention and slow queries indirectly — see query_stats.
        "query_stats": query_stats_snapshot(),
    }


@router.get("/api/health")
async def health():
    """Health check for monitoring and load balancers.

    Uses get_db() which already runs a liveness probe (RETURN 1) every
    _CHECK_INTERVAL seconds and handles reconnection internally.
    No need for a redundant query or aggressive reset_db() here.
    """
    db_ok = False
    try:
        await get_db()
        db_ok = True
    except Exception:
        pass  # WHY: the health endpoint's job is to REPORT db reachability; db_ok stays False and is surfaced in the response.

    # Concurrent: the Redis and driver probes are independent I/O, and running them in
    # series stacked their timeout budgets on the endpoint a load balancer scrapes.
    # gather bounds a full-outage scrape at the SLOWEST probe, not their sum.
    (redis_ok, worker_stamp), agent_lines = await asyncio.gather(
        _probe_redis(), _probe_agent_lines()
    )
    body = _health_body(db_ok, worker_stamp, redis_ok, agent_lines)
    # 503 = out of rotation: reserved for the mandatory dependencies (DB, Redis).
    # Driver loss stays 200 + "degraded" (see _probe_agent_line).
    if not db_ok or not redis_ok:
        return JSONResponse(content=body, status_code=503)
    return body


@router.get("/api/health/storage")
async def health_storage(user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Storage usage breakdown for capacity monitoring (NOT on the hot /api/health path).

    # ARCH: this is a SEPARATE, heavier endpoint — /api/health is scraped
    # frequently by monitoring/load balancers, so a full cp_blobs scan (which
    # aggregates `size` across every row) must NOT run there. Blobs are immortal
    # (user rule) and the table grows unboundedly, so the scan cost grows with it.
    # Scrappers that want storage metrics hit this endpoint at their own cadence.
    # SECURITY: auth-gated (unlike /api/health, which only returns booleans/counters)
    # — the per-row aggregate scan is a DoS amplification surface if exposed
    # unauthenticated, so a session cookie is required. The result is TTL-cached
    # (_STORAGE_CACHE_TTL_SEC) so scrape cadence does not multiply scan cost.
    """
    global _storage_cache, _storage_cache_at
    now = time.monotonic()
    if _storage_cache is not None and (now - _storage_cache_at) < _STORAGE_CACHE_TTL_SEC:
        return _storage_cache

    try:
        rows = await db.query(
            "SELECT count() AS c, math::sum(size) AS s FROM cp_blobs GROUP ALL"
        )
    except Exception:
        # A failing/timeout scan is observable as 503 (not masked zeros) so a
        # degrading aggregate is surfaced rather than hidden.
        return JSONResponse(
            status_code=503,
            content={"detail": "storage_metric_unavailable"},
        )
    row = rows[0] if rows else {}
    body = {
        "cp_blobs_count": row.get("c", 0) or 0,
        # math::sum over a NULL `size` (legacy/edge rows) may yield None.
        "cp_blobs_bytes": row.get("s") or 0,
    }
    _storage_cache = body
    _storage_cache_at = now
    return body
