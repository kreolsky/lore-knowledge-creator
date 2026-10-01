"""Tests for the health check endpoint."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_health_returns_ok(client, test_db):
    # The driver probe pinned to "reachable": a configured-but-absent container
    # would legitimately read "degraded" — not this test's subject. Full-ok
    # composition is asserted here.
    with patch("routes.health._probe_agent_lines",
               new=AsyncMock(return_value={"harness": "reachable"})):
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["db"] == "connected"


@pytest.mark.asyncio
async def test_health_no_auth_required(client, test_db):
    resp = await client.get("/api/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_response_structure(client, test_db):
    data = (await client.get("/api/health")).json()
    assert set(data.keys()) == {
        "status", "db", "redis", "agent_lines", "version",
        "backplane_publish_failures",
        "durability_degraded_count", "query_stats", "code_stamp", "worker_code_stamp",
        "worker_stale", "collab_failure_counters", "event_bus_subscriber_failures",
        "event_bus_subscription_failures",
    }
    # The agent line BY NAME (one entry — the split died with the second line,
    # plan retire-agent-line-a step 4; the legacy `pi` field died with it).
    assert isinstance(data["agent_lines"], dict)
    assert isinstance(data["backplane_publish_failures"], int)
    assert isinstance(data["durability_degraded_count"], int)
    assert isinstance(data["query_stats"], dict)
    assert isinstance(data["code_stamp"], str)
    assert isinstance(data["worker_stale"], bool)
    assert data["redis"] in {"connected", "disconnected"}
    assert all(
        v in {"reachable", "unreachable", "unconfigured"}
        for v in data["agent_lines"].values()
    )
    assert set(data["collab_failure_counters"].keys()) == {"flush_pipeline", "session", "registry"}
    assert all(
        isinstance(data["collab_failure_counters"][k], int)
        for k in ("flush_pipeline", "session", "registry")
    )
    assert isinstance(data["event_bus_subscriber_failures"], int)
    assert isinstance(data["event_bus_subscription_failures"], int)


@pytest.mark.asyncio
async def test_health_reports_release_version(client, test_db):
    """`version` is the release tag the deploy stamped into APP_VERSION.

    Asserted against config, not a literal: the value differs per environment (a real
    tag in prod, the fallback locally), so a literal here would only mirror the default.
    """
    import config

    data = (await client.get("/api/health")).json()
    assert data["version"] == config.APP_VERSION
    assert isinstance(data["version"], str) and data["version"]


@pytest.mark.asyncio
async def test_health_version_available_without_auth(client, test_db):
    """The frontend reads the version from unauthenticated /api/health."""
    data = (await client.get("/api/health")).json()
    assert data["version"]


@pytest.mark.asyncio
async def test_health_worker_stale_flag(client, test_db):
    """worker_stale is True iff the worker's published stamp differs from ours."""
    from code_stamp import CODE_STAMP

    with patch("routes.health._probe_redis", new=AsyncMock(return_value=(True, "deadbeef"))):
        data = (await client.get("/api/health")).json()
    assert data["worker_code_stamp"] == "deadbeef"
    assert data["worker_stale"] is True

    with patch("routes.health._probe_redis", new=AsyncMock(return_value=(True, CODE_STAMP))):
        data = (await client.get("/api/health")).json()
    assert data["worker_stale"] is False

    # No worker has booted yet → stamp absent → not flagged stale.
    with patch("routes.health._probe_redis", new=AsyncMock(return_value=(True, None))):
        data = (await client.get("/api/health")).json()
    assert data["worker_code_stamp"] is None
    assert data["worker_stale"] is False


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
async def test_health_returns_503_when_redis_down(client, test_db):
    """Redis down → 503. The DB path is unchanged: db stays "connected" in the body,
    the 503 names Redis as its own reason (an idle service with dead Redis must not
    read "ok" just because nobody tried to publish)."""
    with patch("routes.health._probe_redis", new=AsyncMock(return_value=(False, None))):
        resp = await client.get("/api/health")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["redis"] == "disconnected"
    assert data["db"] == "connected"


@pytest.mark.asyncio
async def test_health_redis_ok_is_200(client, test_db):
    with patch("routes.health._probe_redis", new=AsyncMock(return_value=(True, None))):
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["redis"] == "connected"


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


@pytest.mark.asyncio
async def test_probes_run_concurrently_not_in_series(monkeypatch):
    """A full-outage scrape is bounded by the SLOWEST probe, not the SUM. In series
    the two timeouts stacked on the load-balancer target."""
    import asyncio
    import time as _time

    import routes.health as health_mod

    async def _slow_redis():
        await asyncio.sleep(0.15)
        return False, None

    async def _db():
        return object()

    monkeypatch.setattr(health_mod, "get_db", _db)
    monkeypatch.setattr(health_mod, "_probe_redis", _slow_redis)
    async def _slow_lines():
        await asyncio.sleep(0.15)
        return {"harness": "unreachable"}

    monkeypatch.setattr(health_mod, "_probe_agent_lines", _slow_lines)
    t0 = _time.monotonic()
    await health_mod.health()
    elapsed = _time.monotonic() - t0
    assert elapsed < 0.28, f"probes ran in series: {elapsed:.3f}s"


# ─── Driver probe — degrades the REPORT (200 + named), never a 503 ───


@pytest.mark.asyncio
async def test_health_driver_down_is_200_degraded_named(client, test_db):
    """The driver unreachable degrades the status to "degraded" but keeps HTTP
    200: the editor and collab work fine without the agent line, and a 503
    would pull the whole service out of rotation over a chat outage."""
    with patch("routes.health._probe_agent_lines",
               new=AsyncMock(return_value={"harness": "unreachable"})):
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["agent_lines"]["harness"] == "unreachable"
    assert "pi" not in data
    assert data["db"] == "connected"


@pytest.mark.asyncio
async def test_health_driver_unconfigured_is_ok(client, test_db):
    """No driver secret configured → "unconfigured", NOT degraded — an unset
    optional dependency is not an outage."""
    with patch("routes.health._probe_agent_lines",
               new=AsyncMock(return_value={"harness": "unconfigured"})):
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    # An unconfigured optional line is a CHOICE, not an outage — status ok.
    assert data["status"] == "ok"
    assert data["agent_lines"] == {"harness": "unconfigured"}


@pytest.mark.asyncio
async def test_health_driver_reachable_is_ok(client, test_db):
    with patch("routes.health._probe_agent_lines",
               new=AsyncMock(return_value={"harness": "reachable"})):
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["agent_lines"] == {"harness": "reachable"}


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
async def test_probe_agent_lines_unconfigured_without_probing(monkeypatch):
    """The probe is gated on the resolved line — an unconfigured line means no
    probe (the health report reads the same resolver as the routing branch)."""
    import routes.health as health_mod

    async def _no_line():
        return None

    monkeypatch.setattr(
        "driver.client.resolve_driver_line", _no_line)
    assert await health_mod._probe_agent_lines() == {"harness": "unconfigured"}


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
