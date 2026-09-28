"""H2 + H1 — turn-lock ownership moved to create_completion (reject-before-create)
and empty-assistant cleanup on terminal error paths.

Before: create_completion batch-created user + empty-assistant rows BEFORE the lock
(acquired deep inside the turn driver). Two concurrent completions both created
rows; one was rejected at the lock → orphan rows. The setup-error terminal paths
also left an empty assistant row.

After: the lock is acquired in create_completion BEFORE the batch create (reject →
no rows), and the placeholder assistant row is deleted on the terminal error paths.
Every turn is driver-owned (plan agent-line-harness-lifecycle step 9: JSON POST
answer + frames on the standing channel) — lock-busy is a plain 409 JSON, whose
open-turn twin lives in test_harness_turn; this file pins the DIRECT pre-hold
variant, the lock's lease mechanics (heartbeat cap, shielded release), and the
pre-hand-off setup-failure cleanup.
"""
import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, patch

import pytest
import routes.chat.completions as comp
import turn_lock as tl  # the lease helpers live here (moved when the pump died)
from helpers import pinned_chat_api

import config


def _LINE():
    from driver.client import DriverLine
    return DriverLine(name="pi", url="http://pi.test", secret="s")


async def _create_session(client, token, pid, doc_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def _list_messages(client, token, sid):
    resp = await client.get(
        f"/api/chat/sessions/{sid}/messages",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_contended_completion_rejected_creates_no_rows(client, admin_user, project_with_doc):
    """H2: a completion that loses the turn lock creates NO message rows (reject
    before create → no orphan user+assistant). The rejection is a plain 409 JSON
    (test_harness_turn pins the open-turn twin; this one pre-holds the lock
    directly, with no channel turn behind it)."""
    from turn_lock import acquire_turn_lock, release_turn_lock

    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)

    # Simulate a turn already in flight by pre-holding the lock.
    tok = await acquire_turn_lock(sid)
    assert tok is not None

    with pinned_chat_api(), \
         patch.object(comp, "check_completion_rate_limit", return_value=True):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 409, resp.text
        assert "already in progress" in resp.json()["detail"]

    await release_turn_lock(sid, tok)
    # The reject-before-create contract: NO rows for this session.
    msgs = await _list_messages(client, token, sid)
    assert msgs == [], f"expected no message rows on contention, got {msgs}"


async def test_turn_lock_store_unavailable_answers_503(
    client, admin_user, project_with_doc, monkeypatch,
):
    """A wedged/unreachable Redis at the lock acquire is 503 'Turn lock store
    unavailable' — never a bare 500 (the pre-step-1 contract: the bounded
    client's TimeoutError raised straight through) and never 409 (no turn is
    in progress; the STORE is down). Mirrors the 409 shape one branch below."""
    import redis.exceptions as redis_exc

    class _WedgedRedis:
        async def set(self, *_a, **_k):
            raise redis_exc.TimeoutError("socket timeout")

    async def _get_redis():
        return _WedgedRedis()

    import redis_pool

    monkeypatch.setattr(redis_pool, "get_redis", _get_redis)

    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)

    with pinned_chat_api(), \
         patch.object(comp, "check_completion_rate_limit", return_value=True):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"] == "Turn lock store unavailable"

    # Same reject-before-create contract as the 409: no rows for this session.
    msgs = await _list_messages(client, token, sid)
    assert msgs == [], f"expected no message rows on store outage, got {msgs}"


# ─── Plan: chat-wedged-after-stop — heartbeat + shielded release ──────────────


class _TtlFakeRedis:
    """In-memory redis with TTL tracking, for the heartbeat driver test."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.expires: dict[str, float] = {}
        self.extend_calls = 0

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.expires[key] = time.monotonic() + ex if ex else 0
        return True

    async def eval(self, script, numkeys, *args):
        key, token = args[0], args[1]
        if len(args) == 3:  # compare-and-extend (heartbeat)
            self.extend_calls += 1
            if self.store.get(key) == token:
                self.expires[key] = time.monotonic() + int(args[2])
                return 1
            return 0
        if self.store.get(key) == token:  # compare-and-delete (release)
            self.store.pop(key, None)
            return 1
        return 0


class _SlowReleaseRedis:
    """Redis whose compare-and-delete release blocks on a gate, so a task
    cancellation landing DURING the release round-trip can be exercised — the
    exact wedge (a cancelled turn task whose teardown release is re-cancelled
    before the Redis round-trip completes)."""

    def __init__(self, token):
        self.store = {"pi:turn-lock:s1": token}
        self.gate = asyncio.Event()
        self.delete_started = asyncio.Event()

    async def eval(self, script, numkeys, *args):
        key = args[0]
        if len(args) == 2:  # compare-and-delete (release path)
            self.delete_started.set()
            await self.gate.wait()
            if self.store.get(key) == args[1]:
                self.store.pop(key, None)
                return 1
            return 0
        return 0


@pytest.mark.asyncio
async def test_heartbeat_extends_the_lock_while_streaming(monkeypatch):
    """The heartbeat driver refreshes THIS holder's lock on a cadence independent
    of frame emission (a turn can emit no frames for tens of seconds)."""
    fake = _TtlFakeRedis()

    async def _get_redis():
        return fake

    import redis_pool
    monkeypatch.setattr(redis_pool, "get_redis", _get_redis)

    from turn_lock import acquire_turn_lock
    token = await acquire_turn_lock("s1")

    monkeypatch.setattr(config, "TURN_LOCK_HEARTBEAT_S", 0.02)
    monkeypatch.setattr(config, "TURN_LOCK_TTL_S", 1)

    hb = asyncio.ensure_future(tl._heartbeat_turn_lock("s1", token))
    await asyncio.sleep(0.07)  # ~3 heartbeat intervals
    with contextlib.suppress(asyncio.CancelledError):
        hb.cancel()
        await hb

    assert fake.extend_calls >= 2, "heartbeat must extend the lock on a cadence"
    assert fake.store.get("pi:turn-lock:s1") == token, "holder's lock preserved"


@pytest.mark.asyncio
async def test_heartbeat_is_bounded_and_self_terminates(monkeypatch):
    """A leaked heartbeat (a turn whose teardown never ran — the channel's
    on_end was lost) must NOT extend the lock forever — it self-terminates after
    ~one turn-timeout window so the short lock TTL can heal the wedge. Without
    the beat cap it would EXPIRE the lock in a loop and defeat the TTL backstop
    (a permanent wedge)."""
    fake = _TtlFakeRedis()

    async def _get_redis():
        return fake

    import redis_pool
    monkeypatch.setattr(redis_pool, "get_redis", _get_redis)

    from turn_lock import acquire_turn_lock
    token = await acquire_turn_lock("s1")

    # Tiny cadence + 1s wall + 0.5s hold cap → max_beats = ceil(1.5/0.02)+1
    # = 76 → ~1.5s total. HOLD_MAX_S is patched too: the beat cap covers
    # TURN_MAX_WALL_S + TURN_HOLD_MAX_S (the held-turn worst case) since the
    # progress-extension split, so the unpatched 1800s default would run
    # ~1800s here.
    monkeypatch.setattr(config, "TURN_LOCK_HEARTBEAT_S", 0.02)
    monkeypatch.setattr(config, "TURN_LOCK_TTL_S", 1)
    monkeypatch.setattr(config, "TURN_MAX_WALL_S", 1.0)
    monkeypatch.setattr(config, "TURN_HOLD_MAX_S", 0.5)

    hb = asyncio.ensure_future(tl._heartbeat_turn_lock("s1", token))
    # A plain while-True would hang here → wait_for times out. The capped loop ends.
    await asyncio.wait_for(hb, timeout=5.0)
    assert hb.done(), "heartbeat must self-terminate after the beat cap"
    assert fake.extend_calls >= 2


@pytest.mark.asyncio
async def test_cancelled_teardown_releases_the_turn_lock(monkeypatch):
    """THE WEDGE: a cancelled task must STILL release the turn lock during
    teardown.

    Without asyncio.shield, the release in the teardown is re-cancelled before
    its Redis round-trip completes and the lock survives until TTL — wedging the
    session (every later message rejected as 'turn lock busy'). The shielded
    release runs the delete as a detached task that survives cancellation."""
    token = "holder-token"
    slow = _SlowReleaseRedis(token)

    async def _get_redis():
        return slow

    import redis_pool
    monkeypatch.setattr(redis_pool, "get_redis", _get_redis)

    async def turn_like():
        # The turn's body ends; the teardown's shielded release runs next. (The
        # wedge is that the caller's task is re-cancelled DURING that release
        # await — the channel's on_end and the failure ladder both land here.)
        try:
            return
        finally:
            await tl._teardown_turn_lock("s1", token, heartbeat=None)

    task = asyncio.ensure_future(turn_like())
    # The teardown's release is now mid-roundtrip (slow eval parked on the gate).
    await asyncio.wait_for(slow.delete_started.wait(), timeout=3.0)
    task.cancel()                      # cancellation lands during the release
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # The shielded release is detached; open the gate and let it complete.
    slow.gate.set()
    await asyncio.sleep(0.05)
    assert "pi:turn-lock:s1" not in slow.store, (
        "shielded release must complete despite the task being cancelled mid-roundtrip"
    )


# ─── Plan: review-fixes-restore-path-timeline, C — pre-hand-off driver outage ──


async def test_pre_stream_line_outage_answers_502_and_orphans_no_row(
    client, admin_user, project_with_doc,
):
    """The message rows are created BEFORE `build_context` RPCs the driver for
    the agent capability. A line outage there landed in the uncaught handler as
    a bare 500 and left an empty assistant row behind — the user saw an
    unexplained failure and a blank turn in the thread on the next read.

    The contract: a named failure carries a named status (502 naming the line,
    the same wording family the in-turn path uses) and leaves no placeholder.
    The user row stays — it is retryable input."""
    from driver.client import DriverLineUnreachable

    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)

    async def boom(*_a, **_kw):
        raise DriverLineUnreachable("pi", "connect refused")

    with patch.object(comp, "resolve_driver_line", AsyncMock(return_value=_LINE())), \
         patch.object(comp, "build_context", boom), \
         pinned_chat_api(), \
         patch.object(comp, "check_completion_rate_limit", return_value=True):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            cookies={"lore_session": token},
        )

    assert resp.status_code == 502, resp.text
    assert "pi" in resp.json()["detail"], resp.text

    msgs = await _list_messages(client, token, sid)
    roles = [m["role"] for m in msgs]
    assert roles.count("assistant") == 0, f"no orphan placeholder, got {roles}"
    assert roles.count("user") == 1, roles

    # The lock is released either way — a failed setup must not wedge the chat.
    from turn_lock import acquire_turn_lock
    assert await acquire_turn_lock(sid) is not None


async def test_pre_stream_unknown_failure_keeps_500_and_still_cleans_up(
    client, admin_user, project_with_doc,
):
    """An unnamed setup failure keeps its 500 (nothing to name) but must not
    leave the placeholder row either — the cleanup is the shared half."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)

    async def boom(*_a, **_kw):
        raise RuntimeError("context assembly exploded")

    with patch.object(comp, "resolve_driver_line", AsyncMock(return_value=_LINE())), \
         patch.object(comp, "build_context", boom), \
         pinned_chat_api(), \
         patch.object(comp, "check_completion_rate_limit", return_value=True):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            cookies={"lore_session": token},
        )
    assert resp.status_code == 500, resp.text

    msgs = await _list_messages(client, token, sid)
    assert [m["role"] for m in msgs].count("assistant") == 0
