"""Tests for the Redis-backed rate limiter (SYSTEM: rate-limiter).

Runs against the REAL per-worker Redis test db (conftest REDIS_URL + per-test
FLUSHDB) — the module is unconditionally Redis-backed, so an in-memory fake
would test nothing about the property the Redis move exists for: the budget is
shared across processes (replicas) and survives a backend restart.
"""

import time

import pytest
import rate_limit
from rate_limit import (
    MAX_ATTEMPTS,
    RateLimiter,
    RedisSlidingWindow,
    _tiers,
    check_api_key_rate_limit,
    check_completion_rate_limit,
    check_mcp_mutating_rate_limit,
    check_mcp_transport_rate_limit,
    check_pin_rate_limit,
    check_rate_limit,
    check_transcribe_rate_limit,
    make_tier,
    reset_rate_limit,
)

# ─── default tier (5 / 60s) — preserved behavior ─────────────────────────────


@pytest.mark.asyncio
async def test_default_allows_under_limit():
    for _ in range(MAX_ATTEMPTS - 1):
        assert await check_rate_limit("ip-1") is True


@pytest.mark.asyncio
async def test_default_blocks_at_limit():
    for _ in range(MAX_ATTEMPTS):
        assert await check_rate_limit("ip-2") is True
    assert await check_rate_limit("ip-2") is False


@pytest.mark.asyncio
async def test_default_different_keys_independent():
    for _ in range(MAX_ATTEMPTS):
        await check_rate_limit("ip-a")
    assert await check_rate_limit("ip-a") is False
    assert await check_rate_limit("ip-b") is True


@pytest.mark.asyncio
async def test_default_reset_clears_counter():
    for _ in range(MAX_ATTEMPTS):
        await check_rate_limit("ip-3")
    assert await check_rate_limit("ip-3") is False
    await reset_rate_limit("ip-3")
    assert await check_rate_limit("ip-3") is True


@pytest.mark.asyncio
async def test_window_eviction_allows_again_after_window():
    # A dedicated 1s-window tier keeps the test fast; the semantics (an event
    # stops counting once it ages out of the window) are tier-independent.
    make_tier("__short__", 1, 1)
    try:
        assert await rate_limit._tiers["__short__"].allow("k") is True
        assert await rate_limit._tiers["__short__"].allow("k") is False
        time.sleep(1.2)
        assert await rate_limit._tiers["__short__"].allow("k") is True
    finally:
        _tiers.pop("__short__", None)


# ─── per-tier max characterization (blocks at exactly the documented cap) ─────


@pytest.mark.asyncio
async def test_api_key_tier_120_per_120s():
    for _ in range(120):
        assert await check_api_key_rate_limit("k-1") is True
    assert await check_api_key_rate_limit("k-1") is False


@pytest.mark.asyncio
async def test_mcp_mutating_tier_30_per_60s():
    for _ in range(30):
        assert await check_mcp_mutating_rate_limit("agent-1") is True
    assert await check_mcp_mutating_rate_limit("agent-1") is False


@pytest.mark.asyncio
async def test_mcp_transport_tier_240_per_60s():
    for _ in range(240):
        assert await check_mcp_transport_rate_limit("hash-1") is True
    assert await check_mcp_transport_rate_limit("hash-1") is False


@pytest.mark.asyncio
async def test_completion_tier_20_per_60s():
    for _ in range(20):
        assert await check_completion_rate_limit("u-1") is True
    assert await check_completion_rate_limit("u-1") is False


@pytest.mark.asyncio
async def test_transcribe_tier_5_per_60s():
    for _ in range(5):
        assert await check_transcribe_rate_limit("u-2") is True
    assert await check_transcribe_rate_limit("u-2") is False


# ─── per-tier (max, window) registry pinning ──────────────────────────────────


@pytest.mark.asyncio
async def test_tier_thresholds_pinned_in_registry():
    assert _tiers["default"].max == 5 and _tiers["default"].window == 60
    assert _tiers["pin_long"].max == 15 and _tiers["pin_long"].window == 900
    assert _tiers["api_key"].max == 120 and _tiers["api_key"].window == 120
    assert _tiers["mcp_mutating"].max == 30 and _tiers["mcp_mutating"].window == 60
    assert _tiers["mcp_transport"].max == 240 and _tiers["mcp_transport"].window == 60
    assert _tiers["completion"].max == 20 and _tiers["completion"].window == 60
    assert _tiers["transcribe"].max == 5 and _tiers["transcribe"].window == 60


# ─── PIN composite (5/60s default tier binds first, 15/15min long tier second) ─


@pytest.mark.asyncio
async def test_pin_blocked_by_default_tier_within_window():
    # Within one 60s window the 5/60s default tier binds before the 15/900s long tier.
    for _ in range(5):
        assert await check_pin_rate_limit("p-1") is True
    assert await check_pin_rate_limit("p-1") is False


@pytest.mark.asyncio
async def test_pin_resets_both_tiers():
    for _ in range(5):
        await check_pin_rate_limit("p-2")
    assert await check_pin_rate_limit("p-2") is False
    await reset_rate_limit("p-2")
    # The registry-iterating reset must clear BOTH underlying tiers.
    assert await check_rate_limit("p-2") is True
    assert await _tiers["pin_long"].allow("p-2") is True


# ─── reset auto-coverage across every registered tier ─────────────────────────


@pytest.mark.asyncio
async def test_reset_clears_every_tier():
    # Exhaust the same key in every tier, then reset — all must allow again.
    while await check_rate_limit("shared") is True:
        pass
    while await check_api_key_rate_limit("shared") is True:
        pass
    while await check_mcp_mutating_rate_limit("shared") is True:
        pass
    while await check_mcp_transport_rate_limit("shared") is True:
        pass
    while await check_completion_rate_limit("shared") is True:
        pass
    while await check_transcribe_rate_limit("shared") is True:
        pass
    await reset_rate_limit("shared")
    assert await check_rate_limit("shared") is True
    assert await check_api_key_rate_limit("shared") is True
    assert await check_mcp_mutating_rate_limit("shared") is True
    assert await check_mcp_transport_rate_limit("shared") is True
    assert await check_completion_rate_limit("shared") is True
    assert await check_transcribe_rate_limit("shared") is True


@pytest.mark.asyncio
async def test_new_tier_auto_covered_by_reset():
    bucket = make_tier("__test_extra__", 2, 60)
    try:
        assert await bucket.allow("x") is True
        assert await bucket.allow("x") is True
        assert await bucket.allow("x") is False
        await reset_rate_limit("x")
        assert await bucket.allow("x") is True
    finally:
        _tiers.pop("__test_extra__", None)


# ─── the property the Redis move exists for: shared budgets ───────────────────


@pytest.mark.asyncio
async def test_two_clients_share_one_budget_through_redis():
    """Two independent tier objects (≈ two replicas) with the same tier name
    share ONE budget in Redis — the second blocks at the COMBINED cap. The
    in-memory predecessor let each process spend its own full bucket."""
    a = RedisSlidingWindow("__shared__", 3, 60)
    b = RedisSlidingWindow("__shared__", 3, 60)
    try:
        assert await a.allow("k") is True
        assert await b.allow("k") is True
        assert await a.allow("k") is True
        # 3 events total across both "replicas" — the next is over the cap,
        # whichever client it comes from.
        assert await b.allow("k") is False
        assert await a.allow("k") is False
    finally:
        await a.reset("k")


@pytest.mark.asyncio
async def test_budget_survives_client_replacement():
    """A fresh client (≈ backend restart) inherits the spent budget — restarts
    no longer reset the counters."""
    a = RedisSlidingWindow("__restart__", 2, 60)
    try:
        assert await a.allow("k") is True
        assert await a.allow("k") is True
        fresh = RedisSlidingWindow("__restart__", 2, 60)
        assert await fresh.allow("k") is False
    finally:
        await a.reset("k")


# ─── fail-closed on Redis error ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allow_fails_closed_on_redis_error(monkeypatch):
    async def _broken():
        raise ConnectionError("Redis down")

    # The tiers reach Redis through redis_pool.get_redis() (the ONE seam).
    import redis_pool

    monkeypatch.setattr(redis_pool, "get_redis", _broken)
    assert await rate_limit._tiers["default"].allow("k") is False
    assert await check_rate_limit("k") is False


# ─── RateLimiter Protocol (the swap point keeps its shape) ────────────────────


@pytest.mark.asyncio
async def test_redis_tier_implements_rate_limiter_protocol():
    b = RedisSlidingWindow("__proto__", 1, 10)
    assert isinstance(b, RateLimiter)
    assert await b.allow("k") is True
    assert await b.allow("k") is False
    await b.reset("k")


# ─── login tiers: per-device + per-account ───────────────────────────────────────


class _FromIP:
    """ASGI wrapper that pins scope["client"] — a login from one source address.

    A sync TestClient with client= spins a portal loop the loop-bound Redis
    singleton cannot serve (rate limiter fails closed); riding the session-loop
    httpx transport with this wrapper varies the address the route keys on."""

    def __init__(self, app, ip: str):
        self.app, self.ip = app, ip

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["client"] = (self.ip, 50000)
        await self.app(scope, receive, send)


def _client_from(app, ip: str):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_FromIP(app, ip)), base_url="http://test"
    )


async def _login(c, email: str, password: str):
    return await c.post("/api/auth/login", json={"email": email, "password": password})


async def test_login_email_tier_caps_one_account_across_addresses(app, regular_user):
    """The per-account tier bounds guesses on ONE email regardless of how
    many client addresses they come from (10/15min), while a different email
    from a fresh address is judged on credentials, not throttle."""
    # 10 failed logins on one email, each from a DISTINCT client address.
    for i in range(10):
        async with _client_from(app, f"198.51.100.{i}") as c:
            assert (await _login(c, "user@test.com", "wrong")).status_code == 401
    # The 11th, from a fresh address: the email tier refuses it.
    async with _client_from(app, "198.51.100.99") as c:
        assert (await _login(c, "user@test.com", "wrong")).status_code == 429
    # A different email from its own fresh address: 401, not 429.
    async with _client_from(app, "198.51.100.100") as c:
        assert (await _login(c, "ghost@test.com", "wrong")).status_code == 401


def _device_jwt(email: str, *, key: str | None = None, exp_offset: int = 3600, typ: str = "device") -> str:
    import jwt

    from config import ALGORITHM, SECRET_KEY

    now = int(time.time())
    return jwt.encode(
        {"typ": typ, "sub": email, "nonce": "f" * 32, "iat": now, "exp": now + exp_offset},
        key or SECRET_KEY, algorithm=ALGORITHM,
    )


async def _exhaust_untrusted(app, email: str):
    """10 wrong guesses on `email` from cookie-less clients, then the 11th is 429."""
    for i in range(10):
        async with _client_from(app, f"198.51.100.{i}") as c:
            assert (await _login(c, email, "wrong")).status_code == 401
    async with _client_from(app, "198.51.100.99") as c:
        assert (await _login(c, email, "wrong")).status_code == 429


async def test_known_device_signs_in_while_strangers_exhausted_the_account(app, regular_user):
    """Strangers' guesses never lock the owner's known device out."""
    async with _client_from(app, "203.0.113.1") as owner:
        assert (await _login(owner, "user@test.com", "userpass")).status_code == 200
        assert owner.cookies.get("lore_device")
        await _exhaust_untrusted(app, "user@test.com")
        assert (await _login(owner, "user@test.com", "userpass")).status_code == 200


async def test_device_cookie_for_another_account_is_untrusted(app, admin_user, regular_user):
    async with _client_from(app, "203.0.113.2") as c:
        assert (await _login(c, "user@test.com", "userpass")).status_code == 200
        await _exhaust_untrusted(app, "admin@test.com")
        assert (await _login(c, "admin@test.com", "adminpass")).status_code == 429


@pytest.mark.parametrize("cookie_kind", ["forged", "expired", "wrong_typ"])
async def test_invalid_device_cookie_is_untrusted(app, regular_user, cookie_kind):
    value = {
        "forged": lambda: _device_jwt("user@test.com", key="not-the-secret-key-at-all-0123456789"),
        "expired": lambda: _device_jwt("user@test.com", exp_offset=-10),
        "wrong_typ": lambda: _device_jwt("user@test.com", typ="session"),
    }[cookie_kind]()
    await _exhaust_untrusted(app, "user@test.com")
    async with _client_from(app, "203.0.113.3") as c:
        c.cookies.set("lore_device", value, path="/api/auth")
        assert (await _login(c, "user@test.com", "userpass")).status_code == 429


async def test_device_counter_is_its_own(app, regular_user):
    """A device's failures throttle that device only — never the untrusted counter."""
    async with _client_from(app, "203.0.113.4") as dev:
        assert (await _login(dev, "user@test.com", "userpass")).status_code == 200
        for _ in range(10):
            assert (await _login(dev, "user@test.com", "wrong")).status_code == 401
        assert (await _login(dev, "user@test.com", "userpass")).status_code == 429
    async with _client_from(app, "203.0.113.5") as other:
        assert (await _login(other, "user@test.com", "userpass")).status_code == 200


async def test_each_success_rotates_device_cookie(app, regular_user):
    async with _client_from(app, "203.0.113.6") as c:
        first = (await _login(c, "user@test.com", "userpass")).cookies.get("lore_device")
        second = (await _login(c, "user@test.com", "userpass")).cookies.get("lore_device")
    assert first and second and first != second


async def test_logout_keeps_device_cookie(app, regular_user):
    async with _client_from(app, "203.0.113.7") as c:
        assert (await _login(c, "user@test.com", "userpass")).status_code == 200
        resp = await c.post("/api/auth/logout")
        assert resp.status_code == 200
        assert not [h for h in resp.headers.get_list("set-cookie") if h.startswith("lore_device=")]
        assert c.cookies.get("lore_device")


async def test_login_has_no_per_address_tier(app):
    """One client address guessing at many accounts is judged per account only."""
    async with _client_from(app, "203.0.113.8") as c:
        for i in range(8):
            assert (await _login(c, f"ghost{i}@test.com", "wrong")).status_code == 401


def test_login_device_tier_thresholds_pinned_in_registry():
    assert _tiers["login_device"].max == 10 and _tiers["login_device"].window == 900


def test_login_email_tier_thresholds_pinned_in_registry():
    assert _tiers["login_email"].max == 10 and _tiers["login_email"].window == 900


def test_login_email_key_never_collides_with_ip_keys():
    # The `login-email:` prefix keeps the account key outside reset_rate_limit's
    # registry sweep semantics: an IP-keyed reset can never address an account
    # budget, and a reset of one account's key exists in no other tier.
    assert (
        _tiers["login_email"]._key("login-email:victim@test.com")
        == "rl:login_email:login-email:victim@test.com"
    )

