"""Per-session turn lock — ONE active turn per chat_session.

# SYSTEM: turn-lock — a real Redis `SET NX EX`.
#
# ARCH: acquired by the backend before invoking the driver on a turn and
# released on turn end (the channel's on_end seam — completions_harness). A
# concurrent /completions on the same chat_session is REJECTED with an
# explicit 409 — no queueing in v1
# TTL-guarded so a backend crash mid-turn does not wedge the
# session forever.
#
# ARCH: the lease helpers — the turn-lock heartbeat, the shielded teardown,
# and the lock-busy telemetry — live HERE with the lock (the lock spans the
# WHOLE driver-owned turn). Stop is not a lock operation: it routes into the
# plugin's cancel arm (POST /stop) and the turn's close releases the lock.
#
# INVARIANT: SET NX EX is a single atomic primitive — there is no read-then-set
# window. Why: a non-atomic check-then-set would let two concurrent turns both see
# "unlocked" and both proceed, violating the one-active-turn contract that the
# harness session relies on (the session store is append-only; two writers on one
# session tree in one turn would produce a garbled leaf).
#
# INVARIANT(corruption): release is a fencing-token compare-and-delete, NOT a bare DELETE.
# Why: if a turn outlives the TTL, its lock expires and a second turn legitimately
# acquires it; a bare DELETE on the first turn's finish would then erase the SECOND
# turn's lock, letting a third concurrent turn in. The per-acquire random token +
# Lua compare-and-delete guarantees a release only ever removes THIS holder's lock.
#
# INVARIANT(corruption): extend (the heartbeat refresh) is a fencing-token compare-and-extend,
# NOT a bare EXPIRE. Why: the TTL is short and a
# heartbeat refreshes it while the turn streams; a turn can outlive its TTL (for example,
# a cancelled release + a slow tool call), after which a newer turn legitimately
# re-acquires the lock. A bare EXPIRE from the first turn's stale heartbeat would
# revive that expired lock and re-break the fencing above. The compare-and-extend
# only refreshes THIS holder's lock, so a stale heartbeat is a harmless no-op.
#
# WHY (patch seams): the turn-budget constants are resolved through
# settings.get at call time HERE — tests patch config (TURN_LOCK_HEARTBEAT_S /
# TURN_LOCK_TTL_S / TURN_MAX_WALL_S / TURN_HOLD_MAX_S), not the completions
# facade, so the beats see the patch (the settings fallback leg reads config
# per call).
"""
import asyncio
import contextlib
import logging
import math
import secrets

import redis_pool
import settings

logger = logging.getLogger(__name__)

# A stored Redis key namespace, not prose — live keys in Redis carry this
# exact prefix. WHY: renaming it would split one session's lock across a
# deploy (the turn in flight holds the old key; the new process reads the
# new one — two turns could run for one session inside that window), so the
# name is pinned as-is; old keys expire with the TTL, no cleanup owed.
_LOCK_KEY_PREFIX = "pi:turn-lock:"

# Sentinel returned by acquire for the no-lock case (empty session id — legacy/Ask
# path). Truthy (so `if not token` still treats busy-None as falsy), but release
# recognises it and no-ops. Why: keeps the acquire→bool-check call site simple while
# still threading a value through for the fencing-aware release.
_NOOP_TOKEN = "__nolock__"

# Lua: delete only if the stored value equals THIS holder's token (fencing).
# Returns 1 on deletion, 0 if the value differs (expired + re-acquired by another,
# or already gone). Atomic in Redis — no GET/DEL race.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# Lua: refresh the TTL only if the stored value equals THIS holder's token
# (fencing for the heartbeat — same shape as the release compare-and-delete).
# Returns 1 on refresh, 0 if the value differs (expired + re-acquired by another,
# or already gone). Atomic in Redis — no GET/EXPIRE race. A bare EXPIRE is
# forbidden (see the extend INVARIANT above): it would revive a lock already
# re-acquired by a newer turn and re-break the fencing.
_EXTEND_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
else
    return 0
end
"""


async def acquire_turn_lock(
    session_id: str, *, ttl: int | None = None,
) -> str | None:
    """Atomically acquire the per-session turn lock.

    `ttl` defaults to the instance-setting TURN_LOCK_TTL_S (env⊕default when no
    override row exists). Returns a fencing token (str) if acquired, or None if
    a turn is already in flight. Uses SET NX EX (atomic — no check-then-set
    window). The token MUST be passed back to release_turn_lock so only THIS
    holder's lock is ever removed.
    """
    if not session_id:
        # No session id (legacy/Ask path) → no lock. The lock only governs the
        # harness path, which always carries a chat_session id.
        return _NOOP_TOKEN
    if ttl is None:
        ttl = await settings.get("TURN_LOCK_TTL_S")
    r = await redis_pool.get_redis()
    key = f"{_LOCK_KEY_PREFIX}{session_id}"
    token = secrets.token_urlsafe(16)
    # SET key token NX EX ttl — single atomic round-trip. The token is the value,
    # so release can distinguish holders (fencing).
    ok = await r.set(key, token, nx=True, ex=ttl)
    return token if ok else None


async def release_turn_lock(session_id: str, token: str | None) -> None:
    """Release the per-session turn lock via fencing-token compare-and-delete.

    Pass the token returned by acquire_turn_lock. Best-effort: a None/_NOOP token
    is a no-op (no lock held); a mismatched/missing key (expired + re-acquired by
    another turn, or already gone) is silently left alone — that is the CORRECT
    behaviour, not an error (see the fencing INVARIANT above).
    """
    if not session_id or not token or token == _NOOP_TOKEN:
        return
    r = await redis_pool.get_redis()
    key = f"{_LOCK_KEY_PREFIX}{session_id}"
    try:
        await r.eval(_RELEASE_SCRIPT, 1, key, token)
    except Exception:  # noqa: BLE001 — release is best-effort; never mask the turn
        logger.warning("turn-lock release failed for %s", session_id, exc_info=True)


async def extend_turn_lock(
    session_id: str, token: str | None, *, ttl: int | None = None,
) -> int:
    """Refresh the per-session turn lock's TTL via fencing-token compare-and-extend.

    `ttl` defaults to the instance-setting TURN_LOCK_TTL_S. Driven by the
    heartbeat (_heartbeat_turn_lock here) while a turn runs: it
    keeps a slow-but-legit turn alive while the TTL stays short (plan:
    chat-wedged-after-stop). The compare-and-extend means a STALE heartbeat — whose
    turn outlived the TTL so a newer turn re-acquired the lock — is a no-op; it
    never revives another holder's lock (see the extend INVARIANT).

    Returns 1 if refreshed, 0 if not held by this token. Best-effort: a None/_NOOP
    token is a no-op (returns 0); Redis errors are swallowed (a missed beat merely
    shortens the safety margin — the short TTL + the next beat recover it).
    """
    if not session_id or not token or token == _NOOP_TOKEN:
        return 0
    if ttl is None:
        ttl = await settings.get("TURN_LOCK_TTL_S")
    r = await redis_pool.get_redis()
    key = f"{_LOCK_KEY_PREFIX}{session_id}"
    try:
        return await r.eval(_EXTEND_SCRIPT, 1, key, token, ttl)
    except Exception:  # noqa: BLE001 — heartbeat is best-effort; never crash the turn
        logger.debug("turn-lock extend failed for %s", session_id, exc_info=True)
        return 0


# ── The lease helpers ────────────────────────────────────────────────────────


async def _heartbeat_turn_lock(session_id: str, token: str) -> None:
    """Refresh THIS holder's turn-lock TTL on a cadence while the turn runs.

    # WHY: driven by the turn path's heartbeat task that is alive for the
    # whole driver-owned turn — NOT by frame emission, because a single tool
    # call can run 10–30 s emitting zero frames (a frame-driven heartbeat
    # would stall exactly then). Each beat is a fencing compare-and-extend
    # (extend_turn_lock above): it only refreshes THIS holder's lock, so a
    # stale beat (its turn outlived a short TTL + a newer turn re-acquired)
    # is a harmless no-op. Why a heartbeat at all: the lock TTL is short
    # (fast self-heal on a leak), so a legitimately slow turn is kept alive
    # by refreshes, not by a long TTL.
    #
    # BOUNDED: the loop is capped at ~one held-turn worst-case window:
    # TURN_MAX_WALL_S + TURN_HOLD_MAX_S (the channel's deadline is
    # progress-extended — a working turn survives up to the absolute wall
    # TURN_MAX_WALL_S; it pauses while a hold awaits the user and the
    # aggregate pause is capped at TURN_HOLD_MAX_S — see
    # driver.channel._HoldPausedDeadline). Without a cap a leaked beat would
    # EXPIRE the lock forever and defeat the short TTL that is the wedge's
    # last line of defence.
    """
    cadence = await settings.get_all([
        "TURN_LOCK_TTL_S", "TURN_LOCK_HEARTBEAT_S",
        "TURN_MAX_WALL_S", "TURN_HOLD_MAX_S",
    ])
    ttl = cadence["TURN_LOCK_TTL_S"]
    heartbeat_s = cadence["TURN_LOCK_HEARTBEAT_S"]
    max_beats = math.ceil(
        (cadence["TURN_MAX_WALL_S"] + cadence["TURN_HOLD_MAX_S"]) / heartbeat_s
    ) + 1
    for _ in range(max_beats):
        await asyncio.sleep(heartbeat_s)
        ok = await extend_turn_lock(session_id, token, ttl=ttl)
        logger.debug("[STOP] HEARTBEAT extend session=%s refreshed=%s", session_id, ok)


async def _teardown_turn_lock(
    session_id: str, token: str | None, heartbeat: asyncio.Task | None
) -> None:
    """Stop the heartbeat and release the turn lock, surviving task cancellation.

    # WHY: on the stop path the caller's task can be cancelled (the browser's
    # POST aborted, a breach tearing down); a plain `await release_turn_lock`
    # in a finally is re-cancelled before its Redis round-trip completes (a
    # single cancel leaves a pending _must_cancel that cancels the release's
    # first await), so the lock survives until its TTL and WEDGES the session
    # (every later message rejected as 'turn lock busy'). The release is
    # therefore shielded: it runs as a detached task that completes despite
    # the cancellation. The short TTL is the backstop for the process-kill
    # case where even the shield cannot run.
    """
    if heartbeat is not None and not heartbeat.done():
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await heartbeat
    logger.debug(
        "[STOP] TURN-END teardown session=%s token_held=%s",
        session_id, bool(token),
    )
    if not token:
        return
    try:
        await asyncio.shield(release_turn_lock(session_id, token))
    except asyncio.CancelledError:
        # The outer task was cancelled; the shielded release keeps running
        # detached and completes regardless. Swallow so the cancel does not mask it.
        pass


async def _record_turn_lock_rejection(
    user: dict, session: dict, session_id: str, assistant_msg_id: str,
) -> None:
    """Observability for a lock-busy rejection — the contention rate stays visible
    on prod; it lives with the lock, not in the turn path."""
    try:
        from driver.persistence import _record_turn_error
        await _record_turn_error(
            assistant_msg_id=assistant_msg_id, session_id=session_id,
            reason="turn_lock_busy", source="turn_lock", kind="turn_lock_rejected",
            user_id=user["user_id"], project_id=session.get("project_id"),
        )
    except Exception:
        logger.warning("turn_lock_rejected telemetry failed", exc_info=True)
