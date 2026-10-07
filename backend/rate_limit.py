"""Redis-backed rate limiter for login and PIN attempts (and every other tier)."""

# SYSTEM: rate-limiter — Redis-backed request rate limiting
#
# ARCH: UNCONDITIONALLY Redis — one impl, no selector between backends, no
# in-memory fallback. Why: there is no deployment without Redis (REDIS_URL is
# _require_env in config.py; backplane pins "Redis down = service down"), so a
# second impl would encode a state the deployment cannot be in. The in-process
# predecessor doubled every tier's effective limit under a second replica and
# silently reset on backend restart — both fixed by sharing one budget in
# Redis, which is the entire point of this module.
#
# INVARIANT(security): fails CLOSED on a Redis error — allow() returns False.
# Why: these tiers guard auth surfaces (login, PIN brute-force); failing open
# would drop protection exactly when the service is already unhealthy.
#
# The client itself lives in redis_pool (SYSTEM: redis-pool) — the shared
# bounded string client, awaited per check; the async/event-loop INVARIANT and
# the socket/health/retry options moved with it.
#
# INVARIANT(security): the window is evaluated with Redis TIME inside one Lua
# script, not
# with a per-process clock. Why: replicas share the limiter, so the clock must
# be shared too — per-process monotonic clocks are not comparable across
# replicas, and an INCR/EXPIRE fixed window would additionally allow a
# boundary-straddling burst (2×max in ~1s) the deleted timestamp-list never
# allowed. The sorted-set window below preserves the old implemented semantics:
# an event stops counting once it ages out of the window.

import logging
from typing import Protocol, runtime_checkable

import redis_pool

logger = logging.getLogger(__name__)

# Default tier: 5 attempts per 60s
MAX_ATTEMPTS = 5
WINDOW_SEC = 60

_KEY_PREFIX = "rl:"

# Lua: atomic sliding-window allow. Redis TIME is the one clock every replica
# agrees on; ZREMRANGEBYSCORE drops events aged out of the window; ZCARD counts
# the remainder; a ZADD + PEXPIRE records the event and self-expires the key
# (no prune pass — the deleted in-memory store's bookkeeping has no analogue).
# Member uniqueness: microsecond-resolution TIME string. Two allows for the
# SAME key within one microsecond would overwrite (under-count by one) — far
# below any tier's granularity; accepted.
_ALLOW_SCRIPT = """
local t = redis.call('TIME')
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms - tonumber(ARGV[1]))
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
    redis.call('ZADD', KEYS[1], now_ms, t[1] .. '-' .. t[2])
    redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[1]) + 1000)
    return 1
end
return 0
"""

@runtime_checkable
class RateLimiter(Protocol):
    """The tier contract. One implementation (Redis) — kept as a Protocol so the
    public check_* call-sites depend on the shape, not the class."""

    async def allow(self, key: str) -> bool: ...


class RedisSlidingWindow:
    """One tier: at most `max` allows per sliding `window` seconds per key.

    Same semantics as the deleted in-memory timestamp list (an event stops
    counting once it ages out of the window), evaluated atomically in Redis so
    every replica shares one budget and a restart inherits the spent window.
    """

    def __init__(self, tier: str, max_count: int, window: int) -> None:
        self.tier = tier
        self.max = max_count
        self.window = window

    def _key(self, key: str) -> str:
        return f"{_KEY_PREFIX}{self.tier}:{key}"

    async def allow(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited.

        Fails CLOSED on any Redis error (INVARIANT above): the auth tiers must
        not open their budgets because the shared store is unreachable."""
        try:
            r = await redis_pool.get_redis()
            return bool(
                await r.eval(
                    _ALLOW_SCRIPT, 1, self._key(key), self.window * 1000, self.max
                )
            )
        except Exception:
            logger.warning(
                "rate-limit tier %s: Redis check failed — failing closed",
                self.tier, exc_info=True,
            )
            return False

    async def reset(self, key: str) -> None:
        """Clear `key`'s budget (successful login/PIN entry resets the tier)."""
        try:
            await (await redis_pool.get_redis()).delete(self._key(key))
        except Exception:
            logger.warning(
                "rate-limit tier %s: Redis reset failed", self.tier, exc_info=True
            )


# Registry of every tier, keyed by tier name. reset iterates this registry so
# adding a tier is one make_tier(...) line — it can never be forgotten by
# reset_rate_limit.
_tiers: dict[str, RedisSlidingWindow] = {}


def make_tier(name: str, max_count: int, window: int) -> RedisSlidingWindow:
    """Register (and return) a rate-limit tier.

    Called once at import for each existing tier; new tiers registered here are
    automatically covered by `reset_rate_limit`."""
    bucket = RedisSlidingWindow(name, max_count, window)
    _tiers[name] = bucket
    return bucket


# ─── Tier registration (one make_tier per existing tier) ─────────────────────

# Default tier: 5 attempts per 60s (the short half of PIN, invites, cabinet).
_default_bucket = make_tier("default", MAX_ATTEMPTS, WINDOW_SEC)

# PIN long tier: 15 attempts per 15 min (the long half of PIN, on top of 5/60s).
_PIN_LONG_MAX = 15
_PIN_LONG_WINDOW = 900  # 15 minutes
_pin_long_bucket = make_tier("pin_long", _PIN_LONG_MAX, _PIN_LONG_WINDOW)

# API key tier: 120 requests per 120s (polling-safe, still brute-force resistant).
_API_KEY_MAX = 120
_API_KEY_WINDOW = 120
_api_key_bucket = make_tier("api_key", _API_KEY_MAX, _API_KEY_WINDOW)

# MCP mutating tier: 30 mutating writes / 60s per agent key. Caps a runaway
# external agent loop (the driver-side guards cover the agent line, not
# MCP agents). Keyed on key_id (stable, survives token rotation). Honest single-
# agent use is well below this. See SYSTEM: rate-limiter + SYSTEM: mcp-gateway.
_MCP_MUTATING_MAX = 30
_MCP_MUTATING_WINDOW = 60
_mcp_mutating_bucket = make_tier("mcp_mutating", _MCP_MUTATING_MAX, _MCP_MUTATING_WINDOW)

# MCP transport tier: 240 requests / 60s per token hash, enforced at the ASGI
# gate BEFORE the stateless session manager runs. Bounds pre-auth POSTs and
# tools/list (which the session manager serves with no per-call DB auth — F3).
# Looser than the per-call api-key tier (120/120s) and the mutating tier (30/60s)
# so it never halves an honest agent's effective budget: it is a flood ceiling,
# not a business limit. Keyed on the full token hash (a bad/anonymous token still
# gets its own bucket). See SYSTEM: rate-limiter + SYSTEM: mcp-gateway.
_MCP_TRANSPORT_MAX = 240
_MCP_TRANSPORT_WINDOW = 60
_mcp_transport_bucket = make_tier("mcp_transport", _MCP_TRANSPORT_MAX, _MCP_TRANSPORT_WINDOW)

# Completion tier: 20 LLM completions per 60s per user.
# LLM requests are expensive — prevents API quota exhaustion.
_COMPLETION_MAX = 20
_COMPLETION_WINDOW = 60
_completion_bucket = make_tier("completion", _COMPLETION_MAX, _COMPLETION_WINDOW)

# Verdict tier: 30 verdict POSTs per 60s per user (mid-turn approval). Each POST
# runs a SurrealDB ownership query + Redis membership/writes; unbounded, an
# authenticated user could hammer it for free DB+Redis load. 30/min is far above
# honest use (one user answers one held call at a time) while bounding the spam.
_VERDICT_MAX = 30
_VERDICT_WINDOW = 60
_verdict_bucket = make_tier("verdict", _VERDICT_MAX, _VERDICT_WINDOW)

# Transcription tier: 5 STT requests per 60s per user.
# STT is synchronous and expensive (minutes per call). Without a request-count limit one
# user can spawn many long-lived requests and monopolize the shared STT_CONCURRENCY worker
# pool for everyone. Keyed on user_id (dictation is one-utterance-at-a-time, so 5/min is
# well above honest use).
_TRANSCRIBE_MAX = 5
_TRANSCRIBE_WINDOW = 60
_transcribe_bucket = make_tier("transcribe", _TRANSCRIBE_MAX, _TRANSCRIBE_WINDOW)

# Login email tier: 10 attempts / 15 min per TARGET ACCOUNT, for clients that
# do NOT hold a valid device cookie for it (see the login_device tier below).
# Keyed on the account, not the source address: the address is only as real as
# the proxy chain makes it, while the account being guessed at stays ONE key.
# Accepted cost: anyone can lock a NEW browser out of a known account for 15
# minutes after 10 bad guesses; the owner's known devices are on their own tier.
# Keyed with the `login-email:` prefix so the account key can never collide
# with an IP key in reset_rate_limit's registry sweep: an IP-keyed reset
# addresses no account budget, and one account's key exists in no other tier.
_LOGIN_EMAIL_MAX = 10
_LOGIN_EMAIL_WINDOW = 900  # 15 minutes


def _login_email_key(email: str) -> str:
    return f"login-email:{email.strip().lower()}"


_login_email_bucket = make_tier("login_email", _LOGIN_EMAIL_MAX, _LOGIN_EMAIL_WINDOW)

# Login device tier: 10 attempts / 15 min per DEVICE NONCE — a client holding a
# valid device cookie for the account it signs in to is throttled only here, so
# strangers exhausting the account's email tier never lock a known device out.
_LOGIN_DEVICE_MAX = 10
_LOGIN_DEVICE_WINDOW = 900  # 15 minutes


def _login_device_key(nonce: str) -> str:
    return f"login-device:{nonce}"


_login_device_bucket = make_tier("login_device", _LOGIN_DEVICE_MAX, _LOGIN_DEVICE_WINDOW)

# Public share tier: REMOVED. The public-share
# router no longer rate-limits — threat-model audit found the per-IP bucket
# protected nothing real (content is by-design public; token is 256-bit entropy;
# the one expensive op is TTL-cached + depth-capped; volumetric DoS is an edge-
# layer concern, not app-layer). See backend/routes/public_share.py::ARCH(no-rate-limit).
# Tier intentionally NOT re-registered here — leaving a stub would invite a
# "just wire it back up" PR that re-introduces the 429-on-honest-readers UX harm.


# ─── Public tier checks (all awaited — see the event-loop INVARIANT) ─────────


async def check_rate_limit(key: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    return await _default_bucket.allow(key)


async def check_api_key_rate_limit(key: str) -> bool:
    """Return True if the request is allowed. Higher limit than login — allows widget polling."""
    return await _api_key_bucket.allow(key)


async def check_mcp_mutating_rate_limit(key_id: str) -> bool:
    """Return True if the mutating MCP write is allowed, False if rate-limited.

    Only mutating tool calls (edit/create/edit_table_cell) are counted — read-only
    calls are never throttled by this tier."""
    return await _mcp_mutating_bucket.allow(key_id)


async def check_mcp_transport_rate_limit(token_hash: str) -> bool:
    """Return True if the MCP transport request is allowed, False if rate-limited.

    Keyed on the FULL token hash. Enforced at the /mcp ASGI gate for EVERY request
    (tools/list, tools/call, malformed) so a pre-auth flood cannot reach the
    session manager. Distinct, looser bucket than the per-call tiers (F3 note:
    avoids double-counting a call against both transport and api-key budgets)."""
    return await _mcp_transport_bucket.allow(token_hash)


async def check_completion_rate_limit(user_id: str) -> bool:
    """Return True if the request is allowed. 20 completions/minute per user."""
    return await _completion_bucket.allow(user_id)


async def check_verdict_rate_limit(user_id: str) -> bool:
    """Return True if the request is allowed. 30 verdict POSTs/minute per user."""
    return await _verdict_bucket.allow(user_id)


async def check_transcribe_rate_limit(user_id: str) -> bool:
    """Return True if the request is allowed. 5 transcriptions/minute per user."""
    return await _transcribe_bucket.allow(user_id)


async def check_login_email_rate_limit(email: str) -> bool:
    """Return True if an untrusted client's login on this ACCOUNT is allowed (10/15min)."""
    return await _login_email_bucket.allow(_login_email_key(email))


async def reset_login_email_rate_limit(email: str) -> None:
    """Clear ONLY this account's email-tier budget (on that account's success)."""
    await _login_email_bucket.reset(_login_email_key(email))


async def check_login_device_rate_limit(nonce: str) -> bool:
    """Return True if a known device's login attempt is allowed (10/15min)."""
    return await _login_device_bucket.allow(_login_device_key(nonce))


async def reset_login_device_rate_limit(nonce: str) -> None:
    """Clear ONLY this device's budget (on that device's success)."""
    await _login_device_bucket.reset(_login_device_key(nonce))


async def check_pin_rate_limit(key: str) -> bool:
    """Two-tier rate limit for PIN verification: 5/60s AND 15/15min.

    INVARIANT: PIN composes TWO buckets — the default tier (5/60s) gates first,
    then the pin_long tier (15/15min). Why: clearing only one tier on reset leaves
    the caller silently throttled by the other. A reset must clear BOTH (handled by
    reset_rate_limit iterating the registry)."""
    if not await check_rate_limit(key):
        return False
    return await _pin_long_bucket.allow(key)


async def reset_rate_limit(key: str) -> None:
    """Clear `key` from EVERY registered tier (auto-covers new tiers)."""
    for bucket in _tiers.values():
        await bucket.reset(key)
