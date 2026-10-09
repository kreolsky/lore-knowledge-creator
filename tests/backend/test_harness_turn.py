"""The driver-owned turn path (plan agent-line-harness-lifecycle step 6; step 9
retired the SSE arm, so this is the ONLY turn path).

The route keeps the shared prep (validation, lock, message rows, seam A,
context) and hands the turn to the driver — bind the assistant row on the
standing channel, emit the preamble frames (ids/sources/warnings) THROUGH the
same listener queue (one writer, one order), POST /followup with the
documented 409-retry, and answer JSON. The turn lock is held until the
channel CLOSES the turn (the on_end bind callback) — reject-before-create
parity. The per-session standing agent key rides the payload.

Everything here runs against FAKES at the module's own seams (the driver
channel's connector/replay, completions_harness.post_followup) — no driver
container; the persistence behind the relay arms is REAL (test DB).
"""
import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import uuid4

import pytest
import routes.chat.completions as comp
from driver.client import DriverLine, DriverLineUnreachable
from helpers import pin_chat_api
from test_driver_channel import FakeConnector, FakeReplay, _chunk, _env, _turn_end


def _LINE():
    return DriverLine(name="harness", url="http://harness:8090", secret="s3cr3t")


class FollowupFake:
    """`completions_harness.post_followup` stand-in: records payloads,
    programmable replies (busy N times, or a hard error)."""

    def __init__(self):
        self.payloads: list[dict] = []
        self.busy_times = 0
        self.error: Exception | None = None

    async def __call__(self, payload: dict, line=None):
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        if self.busy_times > 0:
            self.busy_times -= 1
            from driver.timeline import DriverTurnBusy

            raise DriverTurnBusy("turn_in_progress")
        return {
            "accepted": True,
            "dsh_session_id": "dsh-1",
            "lore_session_id": payload.get("session_id"),
        }


class SessionForkFake:
    """`driver.timeline.post_session_fork` stand-in: records calls, accepts a
    programmable error (the plugin's 422 / secret-mismatch arms)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.error: Exception | None = None

    async def __call__(self, source_dsh_id, seq, new_lore_id, line=None):
        self.calls.append({
            "source_dsh_id": source_dsh_id, "seq": seq,
            "new_lore_id": new_lore_id,
        })
        if self.error is not None:
            raise self.error
        return {"ok": True}


def _reset_fanout() -> None:
    from routes.chat import fanout as chat_fanout

    for entry in list(chat_fanout._pumps.values()):
        entry.task.cancel()
    chat_fanout._pumps.clear()


async def _settle(seconds: float = 0.15) -> None:
    await asyncio.sleep(seconds)


# WHY: a poll returns the moment its condition holds, so its budget only costs
# time on failure. The CI runner is ~5x slower than gray with ~1s DB queries
# under load; a 2s budget timed out a turn teardown that was merely slow.
async def _until(predicate, timeout: float = 10.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


async def _until_async(probe, timeout: float = 10.0) -> None:
    """_until for awaited probes (DB reads, lock acquisition)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not await probe():
        if loop.time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


@pytest.fixture
def driver_line_pinned(monkeypatch):
    """The driver line CONFIGURED, pinned at its source.

    # INVARIANT: every test that reaches resolve_driver_line() through an
    # UNPATCHED consumer (driver.timeline.post_followup / fetch_session_entries,
    # routes reading the timeline) requests this fixture. Why: the line is read
    # from config at call time, so without the pin the test measures the
    # runner's env — green on dev/gray (HARNESS_DRIVER_SECRET in the container
    # env) and "driver line not configured" in CI (run #1280).
    """
    import config

    monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", _LINE().secret)
    monkeypatch.setattr(config, "HARNESS_DRIVER_URL", _LINE().url)


@pytest.fixture
async def harness_env(monkeypatch, driver_line_pinned):
    """The app's driver surfaces faked: channel connector/replay + post_followup."""
    import driver.timeline

    connector = FakeConnector()
    replay = FakeReplay()
    followups = FollowupFake()
    forks = SessionForkFake()
    async def _fake_line():
        return _LINE()

    monkeypatch.setattr(
        driver.channel, "resolve_driver_line", _fake_line)
    monkeypatch.setattr(driver.channel, "_ws_connect", connector)
    monkeypatch.setattr(driver.channel, "fetch_session_entries", replay)
    # GET messages reads the timeline from driver.timeline's OWN namespace
    # (deferred import in routes/chat/messages.py), not the channel's — left
    # unpatched it called the LIVE harness container on gray.
    monkeypatch.setattr(driver.timeline, "fetch_session_entries", replay)

    # build_context reads the per-model capability off the driver
    # (driver.client.agent_capability, read through the owning module's
    # attribute in routes/chat/context.py)
    # — with the line pinned that is a real GET /capability, which on gray hit
    # the LIVE harness with the fake secret (401) and in CI has no host at all.
    async def _cap(*_a, **_k):
        return {"available": True, "vision": False}

    monkeypatch.setattr(driver.client, "agent_capability", _cap)
    monkeypatch.setattr(driver.channel, "_RECONNECT_MIN_S", 0.01)
    monkeypatch.setattr(driver.channel, "_WATCHDOG_TICK_S", 0.01)
    monkeypatch.setattr(driver.channel, "_SUBSCRIBE_ACK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(driver.timeline, "post_followup", followups)
    # The lazy branch seed's RPC (plan chat-branch-sessions): a stamped
    # session's first turn must never reach the LIVE harness from a test.
    monkeypatch.setattr(driver.timeline, "post_session_fork", forks)
    pin_chat_api(monkeypatch)

    async def _ok_rate(*_a, **_k):
        return True

    monkeypatch.setattr(comp, "check_completion_rate_limit", _ok_rate)
    async def _fake_line():
        return _LINE()

    monkeypatch.setattr(comp, "resolve_driver_line", _fake_line)

    # The turns here run as a plain project member on model "test": it is public
    # (SYSTEM: model-access). The gate itself is pinned in test_model_access.py.
    from db import get_db
    await (await get_db()).query(
        "CREATE model_grants CONTENT { model_id: 'test', subject: 'public' }")

    driver.channel._channel = None
    yield SimpleNamespace(
        connector=connector, replay=replay, followups=followups, forks=forks)
    await driver.channel.get_driver_channel().aclose()
    driver.channel._channel = None
    _reset_fanout()


async def _harness_chat(client, pid: str, doc_id: str, token: str) -> str:
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["session_id"]
    return sid


def _open_ws(sync_app, pid: str, token: str):
    ws = sync_app.websocket_connect(
        f"/ws/project/{pid}", cookies={"lore_session": token})
    ctx = ws.__enter__()
    init = json.loads(ctx.receive_text())
    assert init["type"] == "init"
    return ws, ctx


def _body(client_token: str, text: str = "hello", parent_id: str | None = None) -> dict:
    # parent_id: the turn contract's linear-append rule (plan
    # chat-branch-sessions) — null only for a session's FIRST turn; every
    # follow-up names the current tail row.
    return {
        "messages": [{"role": "user", "content": text}],
        **({"parent_id": parent_id} if parent_id else {}),
    }


async def _list_messages(client, token: str, sid: str) -> list[dict]:
    resp = await client.get(
        f"/api/chat/sessions/{sid}/messages", cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _acquirable(sid: str) -> bool:
    from turn_lock import acquire_turn_lock, release_turn_lock

    token = await acquire_turn_lock(sid)
    if token is None:
        return False
    await release_turn_lock(sid, token)
    return True


def _recv_chat_frames(ws_ctx, count: int) -> list[dict]:
    """Collect `count` chat_frame envelopes from a project-WS socket,
    skipping interleaved broadcasts (content_flushed & co. ride the same
    socket)."""
    frames: list[dict] = []
    while len(frames) < count:
        msg = json.loads(ws_ctx.receive_text())
        if msg.get("type") == "chat_frame":
            frames.append(msg)
    return frames


# ─── the accepted turn ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_answers_json_and_feeds_followup(
    sync_app, client, collab_project, harness_env,
):
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)
    env = harness_env

    owner = _open_ws(sync_app, pid, user_token)
    try:
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions", json=_body(user_token),
            cookies={"lore_session": user_token},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["accepted"] is True
        assert body["user_msg_id"] and body["assistant_msg_id"]

        # The followup payload: the /turn contract + the standing session key.
        assert len(env.followups.payloads) == 1
        payload = env.followups.payloads[0]
        assert payload["session_id"] == sid
        assert payload["assistant_msg_id"] == body["assistant_msg_id"]
        assert payload["harness"] is True
        assert payload["model"] == "test"
        assert payload["agent_key"]

        from redis_pool import get_redis

        r = await get_redis()
        cached = await r.get(f"agent_key:session:{sid}")
        assert cached == payload["agent_key"]

        # The preamble reaches the owner BEFORE the driver's frames, through
        # the SAME listener queue (one writer, one order).
        sock = env.connector.sockets[0]
        for frame in (
            {"type": "model_update", "model": "test"},
            _chunk(8, "Hello "),
            _turn_end(10),
        ):
            sock.push(_env(sid, frame))
        # Let the recv → dispatch → pump → send chain flush BEFORE the
        # blocking receive (receive_text parks the test loop; the pump needs
        # loop time to deliver — the test_chat_fanout settling pattern).
        await _settle()

        got = _recv_chat_frames(owner[1], 5)
        assert got[0]["session_id"] == sid
        assert got[0]["frame"]["type"] == "ids"
        assert got[0]["frame"]["assistant_message_id"] == body["assistant_msg_id"]
        assert [g["frame"].get("type") for g in got[1:]] == [
            "model_update", "dsh_event", "dsh_event", "done"]
        assert got[-1]["frame"]["content"] == "Hello"
    finally:
        owner[0].__exit__(None, None, None)

    # The relay arms persisted the turn for real (reload-path parity).
    await _until_async(lambda: _content_landed(client, user_token, sid, "Hello"))
    msgs = await _list_messages(client, user_token, sid)
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant"]
    assert msgs[1]["content"] == "Hello"


@pytest.mark.asyncio
async def test_turn_carries_the_admin_ai_settings_and_never_logs_the_key(
    client, collab_project, harness_env, caplog, test_db,
):
    """A fresh install sets the AI endpoint and key in the admin panel only;
    the next turn carries exactly those to the harness (admin wins over env,
    no restart), and the key reaches no log line."""
    import settings

    pid, doc_id, admin_token, user_token, _a, _u = collab_project
    admin = {
        "AI_API_URL": "http://gw.admin.example/v1",
        "AI_API_KEY": "sk-admin-only-7f3e",
    }
    try:
        for key, value in admin.items():
            resp = await client.put(
                f"/api/admin/settings/{key}", json={"value": value},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200, resp.text
        sid = await _harness_chat(client, pid, doc_id, user_token)
        with caplog.at_level("DEBUG"):
            resp = await client.post(
                f"/api/chat/sessions/{sid}/completions", json=_body(user_token),
                cookies={"lore_session": user_token},
            )
        assert resp.status_code == 200, resp.text
        payload = harness_env.followups.payloads[0]
        assert payload["ai_api_url"] == admin["AI_API_URL"]
        assert payload["ai_api_key"] == admin["AI_API_KEY"]
        assert any("_build_turn_payload" in r.getMessage() for r in caplog.records)
        assert not [r for r in caplog.records if admin["AI_API_KEY"] in r.getMessage()]
    finally:
        await test_db.query("DELETE instance_settings")
        settings.drop_cache()


@pytest.mark.asyncio
async def test_a_turn_with_no_model_names_the_admin_setting(
    client, collab_project, harness_env, test_db,
):
    """A fresh install has no chat model: the turn is refused before the
    harness is called, and the refusal says where to set one."""
    import settings

    pid, doc_id, admin_token, user_token, _a, _u = collab_project
    try:
        resp = await client.put(
            "/api/admin/settings/CHAT_MODEL", json={"value": ""},
            cookies={"lore_session": admin_token},
        )
        assert resp.status_code == 200, resp.text
        resp = await client.post(
            "/api/chat/sessions",
            json={"project_id": pid, "document_id": doc_id},
            cookies={"lore_session": user_token},
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions", json=_body(user_token),
            cookies={"lore_session": user_token},
        )
        assert resp.status_code == 400, resp.text
        assert "Admin panel → Models & APIs" in resp.json()["detail"]
        assert harness_env.followups.payloads == []
    finally:
        await test_db.query("DELETE instance_settings")
        settings.drop_cache()


async def _content_landed(client, token, sid, content) -> bool:
    msgs = await _list_messages(client, token, sid)
    return any(m.get("content") == content for m in msgs)


# ─── the lock spans the whole driver-owned turn ──────────────────────────────


@pytest.mark.asyncio
async def test_lock_held_until_turn_end_rejects_concurrent_send(
    client, collab_project, harness_env,
):
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)
    env = harness_env

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions", json=_body(user_token),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 200, resp.text
    first_assistant = resp.json()["assistant_msg_id"]

    # A concurrent send while the turn is open: rejected BEFORE message rows.
    # parent = the current tail (turn 1's in-flight assistant row) — the real
    # client's shape; the refusal must be the LOCK's, not the tail check's.
    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json=_body(user_token, "2nd", parent_id=first_assistant),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 409, resp.text
    assert "in progress" in resp.json()["detail"]
    msgs = await _list_messages(client, user_token, sid)
    assert len(msgs) == 2  # only turn 1's user + assistant

    # The turn ends through the channel → on_end releases the lock.
    sock = env.connector.sockets[0]
    for frame in (
        {"type": "model_update", "model": "test"},
        _chunk(8, "Hi"),
        _turn_end(10),
    ):
        sock.push(_env(sid, frame))
    await _until_async(lambda: _acquirable(sid))

    # The follow-up turn appends linearly: parent = the ended turn's tail row.
    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json=_body(user_token, "3rd", parent_id=first_assistant),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 200, resp.text


# ─── failure actors ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_followup_busy_after_retries_is_a_409_with_cleanup(
    sync_app, client, collab_project, harness_env,
):
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)
    harness_env.followups.busy_times = 99  # always busy — the retry ladder's end

    owner = _open_ws(sync_app, pid, user_token)
    try:
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions", json=_body(user_token),
            cookies={"lore_session": user_token},
        )
        assert resp.status_code == 409, resp.text

        # Reject-before-create parity as close as the path allows: the
        # placeholder assistant is deleted, the user row is kept (retryable).
        msgs = await _list_messages(client, user_token, sid)
        assert [m["role"] for m in msgs] == ["user"]

        # No silent degradation: the owner saw ids, then error + turn_closed.
        await _settle()
        got = _recv_chat_frames(owner[1], 3)
        assert [g["frame"]["type"] for g in got] == ["ids", "error", "turn_closed"]
    finally:
        owner[0].__exit__(None, None, None)

    assert await _acquirable(sid)


@pytest.mark.asyncio
async def test_channel_unopenable_is_a_502_with_cleanup(
    client, collab_project, harness_env,
):
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)
    harness_env.connector.refuse = True

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions", json=_body(user_token),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 502, resp.text
    assert "unavailable" in resp.json()["detail"].lower()

    msgs = await _list_messages(client, user_token, sid)
    assert [m["role"] for m in msgs] == ["user"]
    assert await _acquirable(sid)


@pytest.mark.asyncio
async def test_unconfigured_line_is_a_503_with_cleanup(
    client, collab_project, harness_env, monkeypatch,
):
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)
    async def _no_line():
        return None

    monkeypatch.setattr(comp, "resolve_driver_line", _no_line)

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions", json=_body(user_token),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 503, resp.text
    msgs = await _list_messages(client, user_token, sid)
    assert [m["role"] for m in msgs] == ["user"]
    assert await _acquirable(sid)


# ─── stop + the lifecycle flip back ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_routes_into_post_stop(
    client, collab_project, harness_env, monkeypatch,
):
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)
    env = harness_env

    import driver.timeline

    stops: list[str] = []

    async def fake_stop(session_id, line=None):
        stops.append(session_id)
        return {"stopped": True, "session_id": session_id}

    monkeypatch.setattr(driver.timeline, "post_stop", fake_stop)

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions", json=_body(user_token),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions/cancel", json={},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cancelled"] is True
    assert stops == [sid]

    # The aborted turn still closes through the channel → lock released.
    sock = env.connector.sockets[0]
    for frame in (
        {"type": "model_update", "model": "test"},
        _turn_end(10, "aborted"),
    ):
        sock.push(_env(sid, frame))
    await _until_async(lambda: _acquirable(sid))


@pytest.mark.asyncio
async def test_cancel_unreachable_line_is_a_502(
    client, collab_project, harness_env, monkeypatch,
):
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)

    import driver.timeline

    async def _down(session_id, line=None):
        raise DriverLineUnreachable("harness", "connection refused")

    monkeypatch.setattr(driver.timeline, "post_stop", _down)

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions/cancel", json={},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 502, resp.text
    assert "unavailable" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_cancel_secret_mismatch_names_the_cause(
    client, collab_project, harness_env, monkeypatch,
):
    """401 on Stop states the cause — different driver secrets, recreate the
    pair — not the generic 'not reachable' wording."""
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)

    import driver.timeline
    from driver.client import DriverSecretMismatch

    async def _mismatch(session_id, line=None):
        raise DriverSecretMismatch("harness")

    monkeypatch.setattr(driver.timeline, "post_stop", _mismatch)

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions/cancel", json={},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert "different driver secrets" in detail
    assert "docker compose up -d" in detail


@pytest.mark.asyncio
async def test_turn_refusal_names_the_secret_mismatch_cause(
    client, collab_project, harness_env,
):
    """A 401 on /followup refuses the turn naming the cause — the same
    wording as Stop — instead of the generic unreachable message."""
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)

    from driver.client import DriverSecretMismatch

    harness_env.followups.error = DriverSecretMismatch("harness")
    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions", json=_body(user_token),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert "different driver secrets" in detail
    assert "docker compose up -d" in detail


# ─── post_followup itself: the 409-retry contract ────────────────────────────


@dataclass
class _FakeReply:
    status_code: int
    payload: dict = field(default_factory=dict)
    text: str = ""

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=None)


class _FakePostClient:
    """httpx.AsyncClient stand-in: a scripted queue of replies per call."""

    replies: list[_FakeReply] = []
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_post_followup_accepts_first_try(monkeypatch, driver_line_pinned, http_pool):
    import driver.timeline

    _FakePostClient.replies = [_FakeReply(200, {"accepted": True})]
    _FakePostClient.calls = []
    http_pool("driver", _FakePostClient())
    monkeypatch.setattr(driver.timeline, "_BUSY_RETRY_DELAYS_S", (0.01,))

    reply = await driver.timeline.post_followup({"session_id": "s1"})
    assert reply["accepted"] is True
    assert len(_FakePostClient.calls) == 1
    assert _FakePostClient.calls[0]["url"].endswith("/followup")


@pytest.mark.asyncio
async def test_post_followup_retries_409_then_succeeds(monkeypatch, driver_line_pinned, http_pool):
    import driver.timeline

    _FakePostClient.replies = [
        _FakeReply(409, {"detail": "turn_in_progress"}),
        _FakeReply(200, {"accepted": True}),
    ]
    _FakePostClient.calls = []
    http_pool("driver", _FakePostClient())
    monkeypatch.setattr(driver.timeline, "_BUSY_RETRY_DELAYS_S", (0.01, 0.01))

    reply = await driver.timeline.post_followup({"session_id": "s1"})
    assert reply["accepted"] is True
    assert len(_FakePostClient.calls) == 2


@pytest.mark.asyncio
async def test_post_followup_busy_forever_raises_turn_busy(monkeypatch, driver_line_pinned, http_pool):
    import driver.timeline

    _FakePostClient.replies = [
        _FakeReply(409, {"detail": "turn_in_progress"}),
        _FakeReply(409, {"detail": "turn_in_progress"}),
        _FakeReply(409, {"detail": "turn_in_progress"}),
    ]
    _FakePostClient.calls = []
    http_pool("driver", _FakePostClient())
    monkeypatch.setattr(driver.timeline, "_BUSY_RETRY_DELAYS_S", (0.01, 0.01))

    from driver.timeline import DriverTurnBusy

    with pytest.raises(DriverTurnBusy):
        await driver.timeline.post_followup({"session_id": "s1"})
    assert len(_FakePostClient.calls) == 3  # initial + both retries


@pytest.mark.asyncio
async def test_post_followup_error_status_is_line_unreachable(monkeypatch, http_pool):
    import driver.timeline

    _FakePostClient.replies = [_FakeReply(500, {})]
    http_pool("driver", _FakePostClient())

    with pytest.raises(DriverLineUnreachable):
        await driver.timeline.post_followup({"session_id": "s1"})


@pytest.mark.asyncio
async def test_post_followup_401_is_a_secret_mismatch(monkeypatch, driver_line_pinned, http_pool):
    """401 is the named subclass, never the generic unreachable: the pair
    holds different driver secrets (plan component-wiring-not-settings)."""
    import driver.timeline
    from driver.client import DriverSecretMismatch

    _FakePostClient.replies = [_FakeReply(401, {"detail": "Driver secret required"})]
    _FakePostClient.calls = []
    http_pool("driver", _FakePostClient())

    with pytest.raises(DriverSecretMismatch):
        await driver.timeline.post_followup({"session_id": "s1"})
    assert len(_FakePostClient.calls) == 1, "401 is never retried"


@pytest.mark.asyncio
async def test_post_stop_401_is_a_secret_mismatch(monkeypatch, driver_line_pinned, http_pool):
    import driver.timeline
    from driver.client import DriverSecretMismatch

    _FakePostClient.replies = [_FakeReply(401, {"detail": "Driver secret required"})]
    http_pool("driver", _FakePostClient())

    with pytest.raises(DriverSecretMismatch):
        await driver.timeline.post_stop("s1")


# ─── the lazy branch seed (plan chat-branch-sessions, step 3) ────────────────


async def _stamped_branch_chat(
    client, test_db, pid, doc_id, token, *, with_seq=True, source="dsh-old",
):
    """A branch session holding a copied prefix (u1 → a1 stamped into the OLD
    log → u2 tail) and the lazy-seed stamp the migration / /branches wrote.
    Returns (sid, tail_row_id)."""
    sid = await _harness_chat(client, pid, doc_id, token)
    u1, a1, u2 = str(uuid4()), str(uuid4()), str(uuid4())
    for mid, fields in (
        (u1, {"role": "user", "parent_id": None}),
        (a1, {"role": "assistant", "parent_id": u1,
              "driver_seq": 5, "driver_session": source}),
        (u2, {"role": "user", "parent_id": a1}),
    ):
        await test_db.query(
            "CREATE type::record('messages', $id) CONTENT $f",
            {"id": mid, "f": {"chat_id": sid, "content": "c", **fields}},
        )
    await test_db.query(
        "UPDATE type::record('chat_sessions', $id) SET "
        "seed_source_session = $src" + (", seed_source_seq = 5" if with_seq else ""),
        {"id": sid, "src": source},
    )
    return sid, u2


async def _seed_row(test_db, sid):
    return (await test_db.query(
        "SELECT seed_source_session, seed_source_seq "
        "FROM type::record('chat_sessions', $id)", {"id": sid}))[0]


async def _close_turn(env, sid: str, seq: int = 10) -> None:
    """End the open turn through the channel → the bind's on_end releases
    the lock (the module ARCH's release seam). The model_update OPENS the
    bound turn first — the channel's dispatch opens a turn only on its first
    model_update/turn-start frame, so a bare turn/end would relay raw and
    never close. `seq` must be NEW per closure — the channel's dedup drops
    any frame at or below the session's last delivered seq."""
    sock = env.connector.sockets[0]
    for frame in (
        {"type": "model_update", "model": "test"},
        _turn_end(seq),
    ):
        sock.push(_env(sid, frame))
    await _until_async(lambda: _acquirable(sid))


@pytest.mark.asyncio
async def test_a_stamped_branch_seeds_once_then_runs_linearly(
    client, collab_project, harness_env, test_db,
):
    """The first completion in a session carrying the stamp swaps it for the
    log itself: POST /session-fork with the stamped pair, clear both fields,
    and the turn runs under the BRANCH's own id."""
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    env = harness_env
    sid, tail = await _stamped_branch_chat(
        client, test_db, pid, doc_id, user_token)

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json=_body(user_token, "branch turn", parent_id=tail),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 200, resp.text

    assert env.forks.calls == [{
        "source_dsh_id": "dsh-old", "seq": 5, "new_lore_id": sid}]
    # The stamp is spent — a later turn never re-seeds.
    row = await _seed_row(test_db, sid)
    assert row.get("seed_source_session") is None
    assert row.get("seed_source_seq") is None
    # The turn ran in the branch's OWN log (dsh id = Lore id).
    assert env.followups.payloads[0]["session_id"] == sid

    await _close_turn(env, sid)


@pytest.mark.asyncio
async def test_a_crash_before_clearing_reposts_into_the_idempotency_guard(
    client, collab_project, harness_env, test_db,
):
    """Fork ok, clear lost (the crash window): the next completion re-POSTs
    the SAME pair — the plugin's already-exists arm answers without
    re-seeding — and the turn still runs linearly."""
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    env = harness_env
    sid, tail = await _stamped_branch_chat(
        client, test_db, pid, doc_id, user_token)

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json=_body(user_token, "first", parent_id=tail),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 200, resp.text
    first_assistant = resp.json()["assistant_msg_id"]
    await _close_turn(env, sid, seq=10)

    # The simulated crash: the seed landed, the field-clear never did.
    await test_db.query(
        "UPDATE type::record('chat_sessions', $id) "
        "SET seed_source_session = 'dsh-old', seed_source_seq = 5",
        {"id": sid},
    )
    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json=_body(user_token, "second", parent_id=first_assistant),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 200, resp.text

    assert len(env.forks.calls) == 2
    assert env.forks.calls[1] == env.forks.calls[0]
    row = await _seed_row(test_db, sid)
    assert row.get("seed_source_session") is None
    assert [p["session_id"] for p in env.followups.payloads] == [sid, sid]

    await _close_turn(env, sid, seq=20)


@pytest.mark.asyncio
async def test_a_branch_without_a_seed_seq_answers_422(
    client, collab_project, harness_env, test_db,
):
    """The migration's 422 marker (seed_source_session, no seq): a lineage
    with model history no row could name a boundary for — today's honest
    refusal, never a silently empty model history."""
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    env = harness_env
    sid, tail = await _stamped_branch_chat(
        client, test_db, pid, doc_id, user_token, with_seq=False)

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json=_body(user_token, "x", parent_id=tail),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 422, resp.text
    assert "resolvable" in resp.json()["detail"]

    # Nothing happened: no RPC, no turn, no rows, the stamp kept, the lock
    # released (a refusal, not a wedged session).
    assert env.forks.calls == []
    assert env.followups.payloads == []
    assert [m["role"] for m in await _list_messages(client, user_token, sid)] \
        == ["user", "assistant", "user"]
    row = await _seed_row(test_db, sid)
    assert row.get("seed_source_session") == "dsh-old"
    assert await _acquirable(sid)


@pytest.mark.asyncio
async def test_an_unresolvable_plugin_boundary_answers_422_and_keeps_the_stamp(
    client, collab_project, harness_env, test_db,
):
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    env = harness_env
    sid, tail = await _stamped_branch_chat(
        client, test_db, pid, doc_id, user_token)
    from driver.timeline import DriverForkUnresolvable

    env.forks.error = DriverForkUnresolvable(
        "branch point not resolvable in session")

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json=_body(user_token, "x", parent_id=tail),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 422, resp.text
    assert "resolvable" in resp.json()["detail"]
    assert len(env.forks.calls) == 1  # the RPC was attempted, honestly refused
    row = await _seed_row(test_db, sid)
    assert row.get("seed_source_session") == "dsh-old"
    assert env.followups.payloads == []
    assert await _acquirable(sid)


# ─── post_session_fork itself: the error mapping ─────────────────────────────


@pytest.mark.asyncio
async def test_post_session_fork_posts_the_triple(monkeypatch, driver_line_pinned, http_pool):
    import driver.timeline

    _FakePostClient.replies = [_FakeReply(200, {"ok": True})]
    _FakePostClient.calls = []
    http_pool("driver", _FakePostClient())

    reply = await driver.timeline.post_session_fork("dsh-old", 5, "branch-1")
    assert reply == {"ok": True}
    assert len(_FakePostClient.calls) == 1
    assert _FakePostClient.calls[0]["url"].endswith("/session-fork")
    assert _FakePostClient.calls[0]["json"] == {
        "source_dsh_id": "dsh-old", "seq": 5, "new_lore_id": "branch-1"}


@pytest.mark.asyncio
async def test_post_session_fork_422_is_unresolvable(monkeypatch, driver_line_pinned, http_pool):
    import driver.timeline
    from driver.timeline import DriverForkUnresolvable

    _FakePostClient.replies = [
        _FakeReply(422, {}, "branch point not resolvable in session")]
    http_pool("driver", _FakePostClient())

    with pytest.raises(DriverForkUnresolvable):
        await driver.timeline.post_session_fork("dsh-old", 5, "branch-1")


@pytest.mark.asyncio
async def test_post_session_fork_401_is_a_secret_mismatch(monkeypatch, driver_line_pinned, http_pool):
    import driver.timeline
    from driver.client import DriverSecretMismatch

    _FakePostClient.replies = [_FakeReply(401, {"detail": "Driver secret required"})]
    http_pool("driver", _FakePostClient())

    with pytest.raises(DriverSecretMismatch):
        await driver.timeline.post_session_fork("dsh-old", 5, "branch-1")


@pytest.mark.asyncio
async def test_post_session_fork_error_status_is_line_unreachable(monkeypatch, http_pool):
    import driver.timeline

    _FakePostClient.replies = [_FakeReply(500, {})]
    http_pool("driver", _FakePostClient())

    with pytest.raises(DriverLineUnreachable):
        await driver.timeline.post_session_fork("dsh-old", 5, "branch-1")
