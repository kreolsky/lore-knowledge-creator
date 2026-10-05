"""One HTTP-client owner — every outbound httpx.AsyncClient comes from here.

# SYSTEM: http-clients — the per-site httpx pool: one factory, one close, both processes
#
# ARCH: one pool, one factory — no module keeps its own httpx singleton.
# `name` is the SITE ("transcription", "embeddings", "docx",
# "media", "comfy", "comfy_prompt", "models_catalog", "driver"); `timeout` is
# the only per-site parameter.
#
# INVARIANT: `timeout` is applied at BUILD time only — a later call with a
# different timeout returns the SAME client unchanged.  Why: the six driver
# sites share the "driver" name with different latency budgets
# (/session-entries 30/10, /stop 10/5, /followup 15/5, /capability 10/5,
# /session-leaf and the verdict forward 10s), so every driver request passes
# its own `timeout=` per call; a pool that rebuilt or re-timed the shared
# client per caller would make a forgotten per-request timeout silently apply
# another site's budget instead of failing visibly.
#
# The pool is process-wide and BOTH processes close it (main.py lifespan,
# jobs/worker._on_worker_shutdown → close_all()). A closed client left in the
# pool is rebuilt on next use — the is_closed guard every former singleton
# carried, so a TestClient lifespan close must not poison the next request.
# Deliberately NOT pooled: routes/health.py (a probe must not share a
# possibly-wedged pool with what it probes) and pipeline/extractor/utils.py
# (one call per worker job — no pool to gain).
"""
import httpx

_clients: dict[str, httpx.AsyncClient] = {}


def get_http_client(name: str, *, timeout: float | httpx.Timeout) -> httpx.AsyncClient:
    """Return the lazily-built client for `name` (see the module ARCH note:
    `timeout` applies at build time only — sites sharing a name pass their own
    per-request `timeout=` on the call)."""
    client = _clients.get(name)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=timeout)
        _clients[name] = client
    return client


async def close_all() -> None:
    """Close every pooled client and empty the pool. Idempotent; a later
    `get_http_client` builds a fresh client."""
    for client in _clients.values():
        if not client.is_closed:
            await client.aclose()
    _clients.clear()
