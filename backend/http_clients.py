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
# /session-fork and the verdict forward 10s), so every driver request passes
# its own `timeout=` per call; a pool that rebuilt or re-timed the shared
# client per caller would make a forgotten per-request timeout silently apply
# another site's budget instead of failing visibly.
#
# The pool is process-wide and BOTH processes close it (main.py lifespan,
# jobs/worker._on_worker_shutdown → close_all()). A closed client left in the
# pool is rebuilt on next use — the is_closed guard every former singleton
# carried, so a TestClient lifespan close must not poison the next request.
# Deliberately NOT pooled: routes/health.py (a probe must not share a
# possibly-wedged pool with what it probes), pipeline/extractor/utils.py
# (one call per worker job — no pool to gain) and `isolated_client` below.
"""
import functools
import ssl

import httpx

_clients: dict[str, httpx.AsyncClient] = {}


@functools.cache
def outbound_tls_context() -> ssl.SSLContext:
    """The one verifying TLS context of `isolated_client` — a per-call client
    must not re-read the CA bundle per call."""
    return httpx.create_ssl_context(trust_env=False)


def isolated_client(*, timeout: float | httpx.Timeout) -> httpx.AsyncClient:
    """A client that lives for ONE exchange; the caller closes it.

    # INVARIANT(security): an exchange carrying a user's credential never shares
    # a client with another exchange.
    # Why: a client keeps a cookie jar and a keep-alive pool, and a dial pinned
    # to an IP keys both by that IP — a shared one sends user A's session cookie
    # with user B's request, and hands host B a TLS connection whose certificate
    # was only checked for host A.
    """
    return httpx.AsyncClient(
        timeout=timeout, verify=outbound_tls_context(), trust_env=False,
    )


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
