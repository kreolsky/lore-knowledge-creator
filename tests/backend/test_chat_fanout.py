"""Chat frame fan-out tests (plan agent-line-harness-lifecycle step 5).

`ensure_fanout` (backend/routes/chat/fanout.py) wires ONE 'harness' chat
session's relayed frames from the driver channel onto the LIFECYCLE project
WS, owner-filtered: the pump subscribes the channel, takes a listener queue,
and forwards every frame enveloped as `{type:'chat_frame', session_id,
frame}` (the frame verbatim — the same dicts the SSE pump writes) through
`ProjectSession.send_to_user` to the session OWNER's sockets. The second
member of the project is outside the delivery set by construction.

What is pinned, in plan language:
- the leak actor: a second project member connected to the same project WS
  receives NOTHING for the owner's session (sentinel technique: the next
  thing both sockets see is a shared broadcast, and only the owner's socket
  shows chat frames in front of it);
- the owner in two tabs receives the fan-out in seq order in both;
- a BOUND turn flows through the relay arms and the `done` frame reaches the
  wire (the SSE-line → frame parity through the fan-out);
- the dual-run flag is the only switch: a 'sse' session, a note row, a
  missing/deleted row → no channel subscribe, no pump;
- backpressure = drop + client resync, never reorder: a listener past the
  channel's queue bound is closed with the None sentinel (the pump exits;
  a later ensure_fanout re-attaches); unsubscribe closes the same way;
- the wiring table in project_ws.py documents the chat_frame kind.
"""

import asyncio
import json
import pathlib

import driver.channel
import pytest
from driver.client import DriverLine
from routes.chat import fanout as chat_fanout
from routes.chat.fanout import ensure_fanout
from test_driver_channel import (
    FakeConnector,
    FakeReplay,
    PersistSpy,
    _chunk,
    _env,
    _turn_end,
    _verdict_ask,
)


async def _settle(seconds: float = 0.15) -> None:
    """Let the channel recv → dispatch → pump → send chain flush."""
    await asyncio.sleep(seconds)


def _reset_fanout() -> None:
    for entry in list(chat_fanout._pumps.values()):
        entry.task.cancel()
    chat_fanout._pumps.clear()


@pytest.fixture
async def wired_channel(monkeypatch):
    """A fresh process-wide channel with fakes at its own seams (the same
    seams test_driver_channel patches). The caller drives frames through
    `connector.sockets[0].push(_env(<dsh id>, frame))`."""
    connector = FakeConnector()
    replay = FakeReplay()
    async def _fake_line():
        return DriverLine("harness", "http://harness:8090", "s3cr3t")

    monkeypatch.setattr(
        driver.channel, "resolve_driver_line", _fake_line,
    )
    monkeypatch.setattr(driver.channel, "_ws_connect", connector)
    monkeypatch.setattr(driver.channel, "fetch_session_entries", replay)
    monkeypatch.setattr(driver.channel, "_RECONNECT_MIN_S", 0.01)
    monkeypatch.setattr(driver.channel, "_WATCHDOG_TICK_S", 0.01)
    driver.channel._channel = None
    yield driver.channel.get_driver_channel(), connector
    await driver.channel.get_driver_channel().aclose()
    driver.channel._channel = None
    _reset_fanout()


async def _harness_chat(client, pid: str, doc_id: str, token: str) -> str:
    """Create an AI chat as `token` and flag it lifecycle='harness'."""
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["session_id"]
    resp = await client.patch(
        f"/api/chat/sessions/{sid}", json={"lifecycle": "harness"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return sid


def _open_ws(sync_app, pid: str, token: str):
    """One connected project-WS client with the init frame drained."""
    ws = sync_app.websocket_connect(
        f"/ws/project/{pid}", cookies={"lore_session": token})
    ctx = ws.__enter__()
    init = json.loads(ctx.receive_text())
    assert init["type"] == "init"
    return ws, ctx


# ─── delivery: order, envelope, and the leak actor ────────────────────────────


class TestFanoutDelivery:
    @pytest.mark.asyncio
    async def test_frames_reach_owner_tabs_in_order_member_gets_nothing(
        self, sync_app, client, collab_project, wired_channel,
    ):
        """The owner's two tabs receive every frame as chat_frame in seq
        order; a second project member's socket receives NOTHING — its next
        message is the shared broadcast sentinel, not a chat frame."""
        pid, _doc, admin_token, user_token, _admin_uid, _user_uid = collab_project
        sid = await _harness_chat(client, pid, _doc, user_token)
        _ch, connector = wired_channel

        owner_a = _open_ws(sync_app, pid, user_token)
        owner_b = _open_ws(sync_app, pid, user_token)
        member = _open_ws(sync_app, pid, admin_token)

        assert await ensure_fanout(sid) is True
        sock = connector.sockets[0]
        for frame in (
            {"type": "model_update", "model": "local/test"},
            _chunk(8, "Hello "),
            _verdict_ask(9),
        ):
            sock.push(_env(sid, frame))
        await _settle()

        expected = [
            {"type": "model_update", "model": "local/test"},
            _chunk(8, "Hello "),
            _verdict_ask(9),
        ]
        for owner in (owner_a, owner_b):
            got = [json.loads(owner[1].receive_text()) for _ in range(3)]
            assert got == [
                {"type": "chat_frame", "session_id": sid, "frame": f}
                for f in expected
            ]

        # The sentinel: a shared broadcast AFTER the fan-out. WS delivery is
        # ordered per socket — the member's NEXT message being the sentinel
        # (and not a chat frame) is the leak assertion.
        from event_bus import emit
        await emit("embedding_degraded", project_id=pid)
        await _settle()
        assert json.loads(member[1].receive_text()) == {
            "type": "embedding_degraded"}
        # And the owner's socket is drained of chat frames: the sentinel is
        # its next message too (nothing else was pending).
        assert json.loads(owner_a[1].receive_text()) == {
            "type": "embedding_degraded"}

        for ws_ctx in (owner_a, owner_b, member):
            ws_ctx[0].__exit__(None, None, None)

    @pytest.mark.asyncio
    async def test_bound_turn_done_frame_reaches_the_wire(
        self, sync_app, client, collab_project, wired_channel, monkeypatch,
    ):
        """A BOUND turn flows through the relay arms: the listener receives
        the SSE wire order (model_update, dsh_event…, done) and the done
        frame — the SAME join finalize persists — reaches the owner's socket
        inside the chat_frame envelope."""
        pid, _doc, _admin_token, user_token, _admin_uid, _user_uid = collab_project
        sid = await _harness_chat(client, pid, _doc, user_token)
        ch, connector = wired_channel
        PersistSpy(monkeypatch)

        owner = _open_ws(sync_app, pid, user_token)
        try:
            assert await ensure_fanout(sid) is True
            ch.bind_turn(sid, assistant_msg_id="m1", user_id="u1")
            sock = connector.sockets[0]
            for frame in (
                {"type": "model_update", "model": "local/test"},
                _chunk(8, "Hello "),
                _turn_end(10, "completed"),
            ):
                sock.push(_env(sid, frame))
            await _settle()

            got = [json.loads(owner[1].receive_text()) for _ in range(4)]
            assert [g["frame"]["type"] for g in got] == [
                "model_update", "dsh_event", "dsh_event", "done"]
            assert got[-1]["frame"]["content"] == "Hello "
            assert got[-1]["session_id"] == sid
        finally:
            owner[0].__exit__(None, None, None)


# ─── who does NOT get a pump (the only noops left) ─────────────────────────────


class TestFanoutGating:
    @pytest.mark.asyncio
    async def test_note_row_is_a_noop(self, client, collab_project, wired_channel):
        """Note chats never ride the owner's project WS — their frames have
        no fan-out consumer (the lifecycle flag died with the SSE arm; the
        row's note-ness is the whole gate)."""
        pid, doc_id, _admin_token, user_token, *_ = collab_project
        resp = await client.post(
            "/api/chat/sessions",
            json={"project_id": pid, "document_id": doc_id, "is_note": True},
            cookies={"lore_session": user_token},
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["session_id"]

        _ch, connector = wired_channel
        assert await ensure_fanout(sid) is False
        assert not connector.sockets
        assert not chat_fanout._pumps

    @pytest.mark.asyncio
    async def test_missing_session_is_a_noop(self, wired_channel):
        _ch, connector = wired_channel
        assert await ensure_fanout("chat_sessions:nope") is False
        assert not connector.sockets

    @pytest.mark.asyncio
    async def test_ensure_is_idempotent_one_pump(
        self, client, collab_project, wired_channel,
    ):
        pid, _doc, _admin_token, user_token, *_ = collab_project
        sid = await _harness_chat(client, pid, _doc, user_token)
        _ch, _connector = wired_channel

        assert await ensure_fanout(sid) is True
        assert await ensure_fanout(sid) is True
        assert len(chat_fanout._pumps) == 1


# ─── backpressure: drop + resync, never reorder ───────────────────────────────


class TestFanoutBackpressure:
    @pytest.mark.asyncio
    async def test_listener_bound_drop_closes_pump_and_reattaches(
        self, sync_app, client, collab_project, wired_channel, monkeypatch,
    ):
        """A listener past the channel's queue bound is dropped with the None
        sentinel: the frames already queued still go out IN ORDER, the frame
        that overflowed is dropped (client resync = the reload path), the pump
        exits, and a later ensure_fanout re-attaches a fresh one."""
        pid, _doc, _admin_token, user_token, *_ = collab_project
        sid = await _harness_chat(client, pid, _doc, user_token)
        _ch, connector = wired_channel
        monkeypatch.setattr(driver.channel, "_LISTENER_QUEUE_BOUND", 2)

        owner = _open_ws(sync_app, pid, user_token)
        try:
            gate = asyncio.Event()
            real_send = _orig_send_to_project_user()

            async def gated_send(project_id, user_id, message):
                await gate.wait()
                await real_send(project_id, user_id, message)

            monkeypatch.setattr(chat_fanout, "send_to_project_user", gated_send)
            assert await ensure_fanout(sid) is True
            sock = connector.sockets[0]
            # The pump is parked on frame 1's send; 2 and 3 fill the queue,
            # the 4th emit passes the bound → drop + sentinel.
            frames = [_chunk(10, "a"), _chunk(11, "b"), _chunk(12, "c"),
                      _chunk(13, "d")]
            for frame in frames:
                sock.push(_env(sid, frame))
            await _settle()
            assert chat_fanout._pumps.get(sid) is not None  # parked, alive

            gate.set()
            await _settle()
            assert not chat_fanout._pumps  # the sentinel closed the pump

            got = [json.loads(owner[1].receive_text()) for _ in range(3)]
            assert [g["frame"]["data"]["message"]["content"][0]["text"] for g in got] == [
                "a", "b", "c"]  # in order; "d" is the dropped overflow

            # Re-attach: the subscription outlived the dropped listener.
            assert await ensure_fanout(sid) is True
            sock.push(_env(sid, _chunk(14, "e")))
            await _settle()
            got = json.loads(owner[1].receive_text())
            assert got["frame"]["data"]["message"]["content"][0]["text"] == "e"
        finally:
            owner[0].__exit__(None, None, None)

    @pytest.mark.asyncio
    async def test_unsubscribe_closes_the_pump(
        self, client, collab_project, wired_channel,
    ):
        pid, _doc, _admin_token, user_token, *_ = collab_project
        sid = await _harness_chat(client, pid, _doc, user_token)
        ch, _connector = wired_channel

        assert await ensure_fanout(sid) is True
        await ch.unsubscribe(sid)
        await _settle()
        assert not chat_fanout._pumps


# ─── the doc contract ─────────────────────────────────────────────────────────


def test_wiring_table_documents_chat_frame():
    """The chat_frame kind is documented in project_ws.py's Event Wiring
    Table (all five columns) — the fan-out is a custom sender, so nothing
    else forces the row to exist."""
    text = (pathlib.Path("/app") / "routes" / "project_ws.py").read_text()
    table = text.split("Event Wiring Table", 1)[1].split(
        "_SUBSCRIPTIONS:", 1)[0]
    assert "chat_frame" in table


def _orig_send_to_project_user():
    from routes.project_ws import send_to_project_user
    return send_to_project_user


# ─── step 6: the driver session id + the explicit stop ────────────────────────


class TestFanoutDriverSessionId:
    @pytest.mark.asyncio
    async def test_channel_subscribes_the_driver_id_envelope_keeps_chat_id(
        self, sync_app, client, collab_project, wired_channel,
    ):
        """A continuation chat (compacted_from) drives under the DRIVER session
        id: the channel subscribes drv-1, frames pushed for its dsh mapping
        reach the owner enveloped with the CHAT session id."""
        pid, _doc, _admin_token, user_token, _admin_uid, _user_uid = collab_project
        sid = await _harness_chat(client, pid, _doc, user_token)
        _ch, connector = wired_channel
        connector._ack_map = {sid: sid, f"drv-{sid}": f"dsh-{sid}"}

        owner = _open_ws(sync_app, pid, user_token)
        try:
            assert await ensure_fanout(sid, driver_session_id=f"drv-{sid}") is True
            sock = connector.sockets[0]
            assert {"type": "subscribe", "session_id": f"drv-{sid}"} in sock.sent

            sock.push(_env(f"dsh-{sid}", _chunk(8, "hey")))
            await _settle()
            got = json.loads(owner[1].receive_text())
            assert got == {
                "type": "chat_frame", "session_id": sid, "frame": _chunk(8, "hey"),
            }
        finally:
            owner[0].__exit__(None, None, None)


class TestFanoutStop:
    @pytest.mark.asyncio
    async def test_stop_unsubscribes_and_removes_the_pump(
        self, sync_app, client, collab_project, wired_channel,
    ):
        from routes.chat.fanout import stop_fanout

        pid, _doc, _admin_token, user_token, _admin_uid, _user_uid = collab_project
        sid = await _harness_chat(client, pid, _doc, user_token)
        ch, connector = wired_channel

        owner = _open_ws(sync_app, pid, user_token)
        try:
            assert await ensure_fanout(sid) is True
            assert sid in chat_fanout._pumps

            await stop_fanout(sid)
            await _settle()
            assert sid not in chat_fanout._pumps
            assert {"type": "unsubscribe", "session_id": sid} in (
                connector.sockets[0].sent)

            # Post-stop frames reach nobody — the sentinel technique: the
            # owner's NEXT message is a shared broadcast, never a chat_frame.
            connector.sockets[0].push(_env(sid, _chunk(9, "late")))
            await _settle()
            from event_bus import emit
            await emit("embedding_degraded", project_id=pid)
            await _settle()
            assert json.loads(owner[1].receive_text()) == {
                "type": "embedding_degraded"}
        finally:
            owner[0].__exit__(None, None, None)

    @pytest.mark.asyncio
    async def test_stop_without_a_pump_is_a_noop(self, wired_channel):
        from routes.chat.fanout import stop_fanout

        _ch, _connector = wired_channel
        await stop_fanout("chat_sessions:never-flagged")  # must not raise
