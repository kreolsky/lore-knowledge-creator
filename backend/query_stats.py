"""Per-site query timing aggregation for the shared DB connection.

# SYSTEM: query-stats — in-process aggregation of SurrealDB query latency by call site
# ARCH: Monotonic counters keyed by a `site` label, never reset. Exposed on
#       /api/health next to backplane_publish_failures. No external metrics stack
#       (Prometheus and its peers) at this scale — /api/health + the prod-debug skill is enough.
# ARCH: For the SurrealDB WS protocol the SDK returns the whole result, so there is no
#       observable "first byte" from Python. We record total_ms (wall time from call to
#       return) only. Contention on the shared connection is read indirectly: a site
#       whose max_ms spikes while its own work is cheap is being blocked behind another.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# A single slow query on the shared connection blocks the rest, so latency spikes
# correlate. 500 ms is a first guess — revisit the threshold after a week of data.
QUERY_WARN_MS = 500.0

# site -> [count, total_ms, max_ms]. Monotonic; never reset.
_stats: dict[str, list[float]] = {}


def record_query(site: str, elapsed_ms: float) -> None:
    """Record one query's wall-clock time under a call-site label."""
    bucket = _stats.setdefault(site, [0.0, 0.0, 0.0])
    bucket[0] += 1
    bucket[1] += elapsed_ms
    if elapsed_ms > bucket[2]:
        bucket[2] = elapsed_ms
    if elapsed_ms > QUERY_WARN_MS:
        logger.warning("Slow DB query: site=%s %.0fms", site, elapsed_ms)


def snapshot() -> dict[str, dict[str, float]]:
    """Return a {site: {count, total_ms, max_ms}} copy for /api/health."""
    return {
        site: {
            "count": int(b[0]),
            "total_ms": round(b[1], 2),
            "max_ms": round(b[2], 2),
        }
        for site, b in _stats.items()
    }


def reset_for_test() -> None:
    """Clear all recorded stats (tests only)."""
    _stats.clear()
