"""Tests for the health check endpoint."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_health_version_available_without_auth(client, test_db):
    """The frontend reads the version from unauthenticated /api/health."""
    data = (await client.get("/api/health")).json()
    assert data["version"]


@pytest.mark.asyncio
async def test_health_returns_503_when_db_down(client, test_db):
    """Health endpoint returns 503 with degraded status when DB is unreachable."""
    with patch("routes.health.get_db", new=AsyncMock(side_effect=ConnectionError("DB down"))):
        resp = await client.get("/api/health")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["db"] == "disconnected"


# ─── Redis liveness — Redis is mandatory (backplane INVARIANT), so its loss is a 503 ───


@pytest.mark.asyncio
async def test_probe_redis_returns_liveness_and_stamp_in_one_get(monkeypatch):
    """ONE GET answers both questions — liveness AND the worker stamp. Asserted on
    the call count, not just the return: two sequential reads of the same key was
    the shape this replaced, and only a counter can fail on its return."""
    import routes.health as health_mod

    calls = []

    class _FakeBP:
        async def get(self, key):
            calls.append(key)
            return b"stamp"

    monkeypatch.setattr(health_mod, "get_backplane", lambda: _FakeBP())
    assert await health_mod._probe_redis() == (True, "stamp")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_probe_redis_alive_with_no_worker_stamp(monkeypatch):
    """No worker has booted (key absent) is NOT a Redis outage."""
    import routes.health as health_mod

    class _EmptyBP:
        async def get(self, key):
            return None

    monkeypatch.setattr(health_mod, "get_backplane", lambda: _EmptyBP())
    assert await health_mod._probe_redis() == (True, None)


@pytest.mark.asyncio
async def test_probe_redis_false_on_backplane_error(monkeypatch):
    import routes.health as health_mod

    class _DeadBP:
        async def get(self, key):
            raise ConnectionError("Redis down")

    monkeypatch.setattr(health_mod, "get_backplane", lambda: _DeadBP())
    assert await health_mod._probe_redis() == (False, None)


@pytest.mark.asyncio
async def test_probe_redis_false_on_timeout(monkeypatch):
    """A WEDGED (not refused) Redis reads as down at the bound, never hangs the
    endpoint a load balancer scrapes."""
    import asyncio

    import routes.health as health_mod

    class _WedgedBP:
        async def get(self, key):
            await asyncio.sleep(10)

    monkeypatch.setattr(health_mod, "get_backplane", lambda: _WedgedBP())
    monkeypatch.setattr(health_mod, "_REDIS_PROBE_TIMEOUT_S", 0.05)
    assert await health_mod._probe_redis() == (False, None)


# ─── Driver probe — degrades the REPORT (200 + named), never a 503 ───


@pytest.mark.asyncio
async def test_health_secret_mismatch_is_degraded_named(client, test_db):
    """A 401 probe answer is its own name, "secret_mismatch" — not
    "unreachable" — and it counts the report down like any down line: the
    service is up and refusing (plan component-wiring-not-settings)."""
    with patch("routes.health._probe_agent_lines",
               new=AsyncMock(return_value={"harness": "secret_mismatch"})):
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["agent_lines"] == {"harness": "secret_mismatch"}


@pytest.mark.asyncio
async def test_probe_agent_line_401_is_secret_mismatch(monkeypatch):
    """The probe maps HTTP 401 to the named state, distinguishing a secret
    mismatch from a dead service."""
    import routes.health as health_mod
    from driver.client import DriverLine

    class _Resp:
        status_code = 401

        def json(self):
            return {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(health_mod.httpx, "AsyncClient", _Client)
    line = DriverLine(name="harness", url="http://drv", secret="s")
    assert await health_mod._probe_agent_line(line) == "secret_mismatch"


@pytest.mark.asyncio
async def test_probe_agent_lines_unreachable_on_dead_url(monkeypatch):
    """A configured line + unreachable endpoint → "unreachable" (bounded by the
    probe timeout)."""
    import routes.health as health_mod
    from driver.client import DriverLine

    async def _dead_line():
        return DriverLine(name="harness", url="http://127.0.0.1:1", secret="s")

    monkeypatch.setattr(
        "driver.client.resolve_driver_line", _dead_line,
    )
    # A closed port on localhost fails fast (connection refused inside the bound).
    assert await health_mod._probe_agent_lines() == {"harness": "unreachable"}
