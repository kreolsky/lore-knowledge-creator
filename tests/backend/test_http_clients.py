"""Unit tests for http_clients — the one per-site httpx.AsyncClient pool.

The pool is the ONLY constructor of outbound clients (plan
dependencies-point-down step 2). What must stay true:
  - one client per name, rebuilt if closed (the is_closed guard — a
    TestClient lifespan close must not leave a closed client in the pool);
  - close_all() closes every name (both processes call it on shutdown);
  - every driver call passes its own timeout= PER REQUEST — the shared
    "driver" client must not pin one latency budget across /followup,
    /stop, /session-entries and /capability.
"""

import httpx
import pytest


@pytest.fixture(autouse=True)
async def _clean_pool():
    """Isolate the real pool per test — every test here builds REAL clients
    (never used for a request) and must not leak one into another test's loop."""
    import http_clients
    yield
    await http_clients.close_all()


# ─── the pool contract ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_name_returns_one_client():
    import http_clients
    a = http_clients.get_http_client("t1", timeout=1.0)
    b = http_clients.get_http_client("t1", timeout=1.0)
    assert a is b


@pytest.mark.asyncio
async def test_different_names_return_different_clients():
    import http_clients
    a = http_clients.get_http_client("t1", timeout=1.0)
    b = http_clients.get_http_client("t2", timeout=1.0)
    assert a is not b


@pytest.mark.asyncio
async def test_closed_client_is_rebuilt():
    import http_clients
    a = http_clients.get_http_client("t1", timeout=1.0)
    await a.aclose()
    assert a.is_closed
    b = http_clients.get_http_client("t1", timeout=1.0)
    assert b is not a
    assert not b.is_closed


@pytest.mark.asyncio
async def test_first_timeout_wins_no_rebuild_on_different_timeout():
    # WHY pinned: the six driver sites share the "driver" name with different
    # budgets — a later caller's timeout must not rebuild the client (dropping
    # the pool) nor re-timeout it under a running request. The per-request
    # timeout= is the CALLER's job, asserted below.
    import http_clients
    a = http_clients.get_http_client("t1", timeout=1.0)
    b = http_clients.get_http_client("t1", timeout=99.0)
    assert a is b
    assert a.timeout == httpx.Timeout(1.0)


@pytest.mark.asyncio
async def test_close_all_closes_every_name():
    import http_clients
    a = http_clients.get_http_client("t1", timeout=1.0)
    b = http_clients.get_http_client("t2", timeout=1.0)
    await http_clients.close_all()
    assert a.is_closed
    assert b.is_closed
    # The pool is empty: a later get builds a fresh client, not the closed one.
    c = http_clients.get_http_client("t1", timeout=1.0)
    assert c is not a
    assert not c.is_closed


# ─── the driver line: per-request timeouts on one shared client ───────────────


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _RecordingDriverClient:
    """httpx.AsyncClient stand-in: records every request's `timeout=` kwarg."""

    def __init__(self):
        self.calls: list[dict] = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "timeout": timeout})
        if url.endswith("/session-entries"):
            return _Resp({"turns": [], "tail_seq": None})
        if url.endswith("/stop"):
            return _Resp({"stopped": False})
        if url.endswith("/followup"):
            return _Resp({"accepted": True, "dsh_session_id": "d", "lore_session_id": "l"})
        raise AssertionError(f"unexpected url {url}")

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "timeout": timeout})
        return _Resp({"vision": False})


@pytest.mark.asyncio
async def test_driver_call_sites_pass_their_exact_timeout_per_request(
    monkeypatch,
):
    """One shared "driver" client, four different budgets — every call must
    carry its own timeout= (plan Decisions: the per-request timeout is what
    keeps /session-entries 30/10 from riding /stop's 10/5 or the reverse)."""
    import driver.client
    import driver.timeline
    import http_clients

    import config

    monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "s")
    monkeypatch.setattr(config, "HARNESS_DRIVER_URL", "http://drv")
    fake = _RecordingDriverClient()
    monkeypatch.setattr(
        http_clients, "get_http_client",
        lambda name, *, timeout=None: fake,
    )

    await driver.timeline.post_followup({"session_id": "s1"})
    await driver.timeline.post_stop("s1")
    await driver.timeline.fetch_session_entries("s1")
    await driver.client.agent_capability("m1")

    by_url = {c["url"]: c["timeout"] for c in fake.calls}
    assert by_url["http://drv/followup"] == httpx.Timeout(15.0, connect=5.0)
    assert by_url["http://drv/stop"] == httpx.Timeout(10.0, connect=5.0)
    assert by_url["http://drv/session-entries"] == httpx.Timeout(30.0, connect=10.0)
    assert by_url["http://drv/capability"] == httpx.Timeout(10.0, connect=5.0)
