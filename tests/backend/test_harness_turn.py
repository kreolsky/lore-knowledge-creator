"""The driver-owned turn path — the ONLY turn path.

The route validates, assembles the context and hands the send to the driver:
POST /followup queues it in the session's dsh inbox and the route answers
JSON. No row exists until dsh CLAIMS the message: the plugin's `lore/claim`
carries the send's prep facts back, and the channel mints the user +
assistant pair and emits the preamble (ids/sources/warnings) there. A send
during a running turn is never refused — it queues. The per-session standing
agent key rides the payload.

Everything here runs against FAKES at the module's own seams (the driver
channel's connector/replay, completions_harness.post_followup) — no driver
container; the persistence behind the relay arms is REAL (test DB).
"""
import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import uuid4

import pytest
import routes.chat.completions as comp
from driver.client import DriverLine, DriverLineUnreachable
from driver.timeline import fetch_session_status as _real_fetch_session_status
from helpers import pin_chat_api
from test_driver_channel import FakeConnector, FakeReplay, _env, _turn_end


def _LINE():
    return DriverLine(name="harness", url="http://harness:8090", secret="s3cr3t")


class FollowupFake:
    """`completions_harness.post_followup` stand-in: records payloads,
    programmable failure. Every accepted send gets its own dsh message id."""

    def __init__(self):
        self.payloads: list[dict] = []
        self.error: Exception | None = None

    async def __call__(self, payload: dict, line=None):
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return {
            "accepted": True,
            "dsh_session_id": "dsh-1",
            "message_id": f"dm-{len(self.payloads)}",
            "lore_session_id": payload.get("session_id"),
        }


class StatusFake:
    """`driver.timeline.fetch_session_status` stand-in: the live agent's
    status and queue, programmable per test."""

    def __init__(self):
        self.status = "idle"
        self.queue: list = []
        self.seq: int | None = None
        self.calls: list[str] = []

    async def __call__(self, driver_session_id, line=None):
        self.calls.append(driver_session_id)
        return {"status": self.status, "queue": list(self.queue), "seq": self.seq}


def _claim_of(payload: dict, message_id: str = "dm-1") -> dict:
    """The plugin's `lore/claim` for a recorded send — the prep facts back."""
    return {
        "type": "lore/claim", "message_id": message_id,
        **{k: payload[k] for k in (
            "user_msg_id", "assistant_msg_id", "user_text", "images",
            "sources", "warnings")},
        "model": payload["model"],
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
    statuses = StatusFake()
    async def _fake_line():
        return _LINE()

    monkeypatch.setattr(
        driver.channel, "resolve_driver_line", _fake_line)
    monkeypatch.setattr(driver.channel, "_ws_connect", connector)
    monkeypatch.setattr(driver.channel, "fetch_session_entries", replay)
    # GET messages reads the timeline from driver.timeline's OWN namespace
    # (deferred import in routes/chat/timeline_attach.py), not the channel's — left
    # unpatched it called the LIVE harness container on gray.
    monkeypatch.setattr(driver.timeline, "fetch_session_entries", replay)
    monkeypatch.setattr(driver.timeline, "fetch_session_status", statuses)

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
        connector=connector, replay=replay, followups=followups, forks=forks,
        statuses=statuses)
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


def _body(client_token: str, text: str = "hello", parent_id: str | None = None,
          user_msg_id: str | None = None) -> dict:
    # parent_id: the client's tail hint — read only for the root-turn time
    # stamp; user_msg_id: the browser-minted id of the send's user row.
    return {
        "messages": [{"role": "user", "content": text}],
        "user_msg_id": user_msg_id or str(uuid4()),
        **({"parent_id": parent_id} if parent_id else {}),
    }


async def _list_messages(client, token: str, sid: str) -> list[dict]:
    resp = await client.get(
        f"/api/chat/sessions/{sid}/messages", cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["messages"]


# ─── the accepted turn ────────────────────────────────────────────────────────


async def _content_landed(client, token, sid, content) -> bool:
    msgs = await _list_messages(client, token, sid)
    return any(m.get("content") == content for m in msgs)


# ─── the reload: open turn and queue from dsh's status ───────────────────────


@pytest.mark.asyncio
async def test_a_running_agent_marks_the_unlogged_turn_open(
    client, collab_project, harness_env,
):
    """Between the claim and dsh's log flush the timeline cannot show the
    turn — the running status marks it, or a reload closes a live turn."""
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)
    env = harness_env
    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions", json=_body(user_token),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 200, resp.text
    env.connector.sockets[0].push(_env(sid, _claim_of(env.followups.payloads[0])))
    await _until_async(lambda: _content_landed(client, user_token, sid, "hello"))

    env.statuses.status = "running"
    running = await _list_messages(client, user_token, sid)
    assert running[1]["open_turn"] is True and running[1]["frames"] == []

    env.statuses.status = "idle"
    idle = await _list_messages(client, user_token, sid)
    assert "open_turn" not in idle[1]


@pytest.mark.asyncio
async def test_an_unreadable_status_is_a_502(client, collab_project, harness_env, monkeypatch):
    import driver.timeline
    from driver.timeline import DriverTimelineUnavailable

    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)

    async def _down(driver_session_id, line=None):
        raise DriverTimelineUnavailable("connection refused")

    monkeypatch.setattr(driver.timeline, "fetch_session_status", _down)
    resp = await client.get(
        f"/api/chat/sessions/{sid}/messages", cookies={"lore_session": user_token})
    assert resp.status_code == 502, resp.text


# ─── ✕ on a queued message ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discard_removes_the_queued_message(
    client, collab_project, harness_env, http_pool,
):
    """✕ through the real route and the real driver client: the driver's
    answer comes back as is — a message claimed in the meantime is
    `removed: false`, not an error (its turn is the user's feedback)."""
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)
    _FakePostClient.replies = [
        _FakeReply(200, {"removed": True}), _FakeReply(200, {"removed": False})]
    _FakePostClient.calls = []
    http_pool("driver", _FakePostClient())

    for message_id, removed in (("dm-1", True), ("dm-claimed", False)):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/inbox/discard",
            json={"message_id": message_id}, cookies={"lore_session": user_token},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"removed": removed}
    assert [c["json"] for c in _FakePostClient.calls] == [
        {"session_id": sid, "message_id": "dm-1"},
        {"session_id": sid, "message_id": "dm-claimed"},
    ]
    assert _FakePostClient.calls[0]["url"].endswith("/inbox/discard")


@pytest.mark.asyncio
async def test_discard_is_owner_only(client, collab_project, harness_env, http_pool):
    """Another project member cannot ✕ the owner's queued message — the
    driver is never asked."""
    pid, doc_id, admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)
    _FakePostClient.replies = []
    _FakePostClient.calls = []
    http_pool("driver", _FakePostClient())

    resp = await client.post(
        f"/api/chat/sessions/{sid}/inbox/discard",
        json={"message_id": "dm-1"}, cookies={"lore_session": admin_token},
    )
    assert resp.status_code in (403, 404), resp.text
    assert _FakePostClient.calls == []


# ─── failure actors ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_channel_unopenable_is_a_502_with_no_rows(
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

    harness_env.connector.refuse = False
    assert await _list_messages(client, user_token, sid) == []


# ─── stop + the lifecycle flip back ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_routes_into_post_stop(
    client, collab_project, harness_env, monkeypatch,
):
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)

    import driver.timeline

    stops: list[tuple[str, bool]] = []
    returned = [{"text": "queued B", "images": ["data:image/png;base64,AAA"]}]

    async def fake_stop(session_id, line=None, *, return_queue=False):
        stops.append((session_id, return_queue))
        return {"stopped": True, "returned": returned, "session_id": session_id}

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
    # The person's Stop takes the queue back for the composer.
    assert stops == [(sid, True)]
    assert resp.json()["returned"] == returned


@pytest.mark.asyncio
async def test_cancel_unreachable_line_is_a_502(
    client, collab_project, harness_env, monkeypatch,
):
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)

    import driver.timeline

    async def _down(session_id, line=None, **_):
        raise DriverLineUnreachable("harness", "connection refused")

    monkeypatch.setattr(driver.timeline, "post_stop", _down)

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions/cancel", json={},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 502, resp.text
    assert "unavailable" in resp.json()["detail"].lower()


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

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_post_followup_422_is_the_refusal_with_its_reason(
        monkeypatch, driver_line_pinned, http_pool):
    import driver.timeline
    from driver.timeline import DriverTurnRefused

    _FakePostClient.replies = [_FakeReply(422, {"detail": "no gateway configured"})]
    _FakePostClient.calls = []
    http_pool("driver", _FakePostClient())

    with pytest.raises(DriverTurnRefused) as exc:
        await driver.timeline.post_followup({"session_id": "s1"})
    assert str(exc.value) == "no gateway configured"
    assert len(_FakePostClient.calls) == 1


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
    assert len(_FakePostClient.calls) == 1


@pytest.mark.asyncio
async def test_fetch_session_status_wire_shape_and_failure(driver_line_pinned, http_pool):
    import driver.timeline
    from driver.timeline import DriverTimelineUnavailable

    queued = [{"id": "dm-1"}]
    _FakePostClient.replies = [
        _FakeReply(200, {"status": "running", "queue": queued, "seq": 12}),
        _FakeReply(500, {}),
    ]
    _FakePostClient.calls = []
    http_pool("driver", _FakePostClient())

    assert await _real_fetch_session_status("s1") == {
        "status": "running", "queue": queued, "seq": 12}
    assert _FakePostClient.calls[0]["url"].endswith("/session-status")
    assert _FakePostClient.calls[0]["params"] == {"session_id": "s1"}
    with pytest.raises(DriverTimelineUnavailable):
        await _real_fetch_session_status("s1")
    assert driver.timeline.fetch_session_status is not _real_fetch_session_status, (
        "the autouse idle pin is active — this test must call the real reader"
    )


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
    """Run the newest send's turn through the channel: its claim (which
    mints the pair and opens the turn), then its end. `seq` must be NEW per
    closure — the channel's dedup drops any frame at or below the session's
    last delivered seq."""
    sock = env.connector.sockets[0]
    payload = env.followups.payloads[-1]
    for frame in (_claim_of(payload), _turn_end(seq)):
        sock.push(_env(sid, frame))
    await _until_async(lambda: _row_stamped(payload["assistant_msg_id"], seq))


async def _row_stamped(assistant_msg_id: str, seq: int) -> bool:
    """Whether the claimed turn's assistant row exists and carries its end."""
    from db import get_db

    rows = await (await get_db()).query(
        "SELECT driver_seq FROM type::record('messages', $id)",
        {"id": assistant_msg_id},
    ) or []
    return bool(rows) and rows[0].get("driver_seq") == seq


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
