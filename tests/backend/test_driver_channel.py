"""Driver event channel tests (plan agent-line-harness-lifecycle step 4).

`DriverChannel` (backend/driver.channel.py) is the backend's standing WS
subscription to the driver's `/ws/events` endpoint: ONE socket, subscribe per
lore session id, frames arriving ENVELOPED (`{type:'session_frame',
session_id:<dsh id>, frame}` — the envelope is step 4's amendment to the step-2
channel: map.ts frames carry no session attribution, so a shared socket could
not route). Everything here runs against FAKES at the module's own seams
(`_ws_connect`, `fetch_session_entries`, `post_stop`, the rebindable persist
globals) — no driver container, no real WS.

What is pinned, in plan language:
- fresh subscribe ANCHORS at the log tail (first delivery has a dedup anchor);
- received frames flow through the EXISTING relay arms (`_relay_frame`) —
  accumulation, the done frame, persistence — OUTSIDE the pump;
- reconnect → resubscribe → REPLAY from the last delivered seq (the step-1
  `since_seq` projection), dedup on the overlap, the open turn's projection
  CONTINUES (content joins across the gap);
- the no-progress deadline: silence breaches → error frame + abnormal persist
  + best-effort /stop (the pump's TimeoutError branch semantics); a held turn
  (verdict-ask) pauses the same way the pump pauses it;
- unsubscribe mid-turn = the pump's client-disconnect semantics (partial +
  `disconnected` halt) + /stop;
- frames for a turn nobody bound relay WITHOUT persistence.
"""

import asyncio
import json

import driver.channel
import pytest
from driver.channel import DriverChannel
from driver.client import DriverLine

import config

# ─── dsh vocabulary builders (shapes pinned by test_driver_frames.py) ─────────


def _dsh(seq, kind, data=None, **extra):
    return {"type": "dsh_event", "kind": kind, "seq": seq, "data": data, **extra}


def _chunk(seq, text, turn=1):
    # v3: the settled assistant/message (whole step text) — assistant/chunk
    # died with the old format.
    return _dsh(seq, "assistant/message", {
        "turn": turn, "step": 1,
        "message": {"id": f"m{seq}", "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "source": {"kind": "model"}},
        "stream": []}, surfaceOp="append")


def _turn_end(seq, kind="completed", turn=1):
    return _dsh(seq, "turn/end", {"turn": turn, "reason": {"kind": kind}})


def _verdict_ask(seq, call_id="c1", turn=1):
    return {"type": "lore/verdict-ask", "seq": seq, "data": {
        "turn": turn, "callId": call_id, "toolName": "edit_document"}, "ignorable": True}


def _tool_result(seq, call_id="c1", turn=1):
    return _dsh(seq, "tool/result", {"turn": turn, "step": 1, "message": {
        "role": "tool", "source": {"kind": "tool", "callId": call_id},
        "toolCallId": call_id, "content": [{"type": "text", "text": "ok"}]}})


def _env(dsh_id, frame):
    """One enveloped channel frame, as the driver's /ws/events delivers it."""
    return {"type": "session_frame", "session_id": dsh_id, "frame": frame}


def _sub_ack(lore, dsh):
    return {"type": "subscribed", "session_id": lore, "dsh_session_id": dsh}


# ─── fakes at the channel's own seams ─────────────────────────────────────────


class FakeSocket:
    """The transport seam's return: one backend→driver socket. Subscribe and
    unsubscribe sends are ACKED like the real channel does (the mapping lore
    → dsh comes from the connector), so a healthy driver never blocks the
    channel's ack waits."""

    def __init__(self, ack_map: dict[str, str] | None = None):
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._ack_map = ack_map or {}
        self.closed = False

    async def send(self, data: str) -> None:
        msg = json.loads(data)
        self.sent.append(msg)
        if msg.get("type") == "subscribe":
            session_id = msg["session_id"]
            self._inbox.put_nowait(_sub_ack(
                session_id, self._ack_map.get(session_id, session_id)))
        elif msg.get("type") == "unsubscribe":
            self._inbox.put_nowait(
                {"type": "unsubscribed", "session_id": msg["session_id"]})

    async def recv(self) -> str:
        item = await self._inbox.get()
        if item is None:
            raise ConnectionError("connection closed")
        return item if isinstance(item, str) else json.dumps(item)

    async def close(self) -> None:
        self.closed = True

    # test side
    def push(self, msg: dict) -> None:
        self._inbox.put_nowait(msg)

    def drop(self) -> None:
        self._inbox.put_nowait(None)


class FakeConnector:
    """`driver.channel._ws_connect` stand-in: hands out sockets in order."""

    def __init__(self, ack_map: dict[str, str] | None = None):
        self.sockets: list[FakeSocket] = []
        self._ack_map = ack_map or {"lore-1": "dsh-9", "lore-2": "dsh-2"}
        self.url: str | None = None
        self.headers: dict | None = None
        self.refuse = False

    async def __call__(self, url: str, headers: dict):
        if self.refuse:
            raise ConnectionError("driver unreachable")
        self.url, self.headers = url, headers
        sock = FakeSocket(self._ack_map)
        self.sockets.append(sock)
        return sock


class FakeReplay:
    """`fetch_session_entries` stand-in: records (session_id, since_seq) calls,
    serves per-session replies."""

    def __init__(self):
        self.calls: list[dict] = []
        self.replies: dict[str, dict] = {}

    def reply(self, session_id: str, payload: dict) -> None:
        self.replies[session_id] = payload

    async def __call__(self, session_id, line=None, since_seq=None):
        self.calls.append({
            "session_id": session_id, "since_seq": since_seq,
        })
        payload = self.replies.get(session_id)
        if payload is None:
            return {"turns": [], "tail_seq": None}
        return payload


async def _drain(queue: asyncio.Queue) -> list[dict]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


async def _recv(queue: asyncio.Queue, timeout: float = 2.0) -> dict:
    return await asyncio.wait_for(queue.get(), timeout)


async def _until(predicate, timeout: float = 2.0) -> None:
    """Await an asynchronous side effect (a persist racing the last emitted
    frame): the emit happens BEFORE the projection's terminal writes."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


def _frame_of(line: str) -> dict:
    assert line.startswith("data: ")
    return json.loads(line[len("data: "):].strip())


class PersistSpy:
    """Rebinds the channel's persist globals with recorders."""

    def __init__(self, monkeypatch) -> None:
        self.content: list[tuple] = []
        self.sources: list[tuple] = []
        self.extras: list[tuple] = []
        self.turn_seq: list[tuple] = []
        self.turn_session: list[tuple] = []
        self.context_usage: list[tuple] = []
        self.turn_errors: list[dict] = []
        self.titles: list[tuple] = []
        self.stops: list[str] = []

        async def content(mid, content):
            self.content.append((mid, content))

        async def sources(mid, srcs):
            self.sources.append((mid, srcs))

        async def extras(mid, fields):
            self.extras.append((mid, fields))

        async def turn_seq(mid, seq, dsh_session):
            self.turn_seq.append((mid, seq))
            self.turn_session.append((mid, dsh_session))

        async def context_usage(sid, used):
            self.context_usage.append((sid, used))

        async def turn_error(**kw):
            self.turn_errors.append(kw)

        async def stop(sid, line=None):
            self.stops.append(sid)

        async def title(sid, text):
            self.titles.append((sid, text))
            return True

        monkeypatch.setattr(driver.persistence, "_persist_content", content)
        monkeypatch.setattr(driver.persistence, "_persist_sources", sources)
        monkeypatch.setattr(driver.persistence, "_persist_projection_extras", extras)
        monkeypatch.setattr(driver.persistence, "_persist_turn_seq", turn_seq)
        monkeypatch.setattr(driver.persistence, "_persist_context_usage", context_usage)
        monkeypatch.setattr(driver.persistence, "_record_turn_error", turn_error)
        monkeypatch.setattr(driver.persistence, "_persist_session_title", title)
        monkeypatch.setattr(driver.channel, "post_stop", stop)


@pytest.fixture
async def channel(monkeypatch):
    """A wired channel: fake connector + fake replay + recording persists +
    fast reconnect/watchdog cadence. The caller owns aclose()."""
    connector = FakeConnector()
    replay = FakeReplay()
    persists = PersistSpy(monkeypatch)
    async def _line():
        return DriverLine("harness", "http://harness:8090", "s3cr3t")

    monkeypatch.setattr(driver.channel, "resolve_driver_line", _line)
    monkeypatch.setattr(driver.channel, "_ws_connect", connector)
    monkeypatch.setattr(driver.channel, "fetch_session_entries", replay)
    monkeypatch.setattr(driver.channel, "_RECONNECT_MIN_S", 0.01)
    monkeypatch.setattr(driver.channel, "_WATCHDOG_TICK_S", 0.01)
    ch = DriverChannel()
    yield ch, connector, replay, persists
    await ch.aclose()


# ─── subscribe: the anchor and the ack ────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_subscribe_anchors_at_log_tail_and_dedups(channel):
    ch, connector, replay, _ = channel
    replay.reply("lore-1", {"turns": [{"frames": [_chunk(6, "old")]}, {
        "frames": [_chunk(7, "stale")], "end_seq": 7}], "tail_seq": 7})

    await ch.subscribe("lore-1")

    sock = connector.sockets[0]
    assert {"type": "subscribe", "session_id": "lore-1"} in sock.sent
    assert connector.url == "ws://harness:8090/ws/events"
    assert connector.headers == {"X-Driver-Secret": "s3cr3t"}
    # The ANCHOR fetch ran WITHOUT a since_seq (nothing was delivered yet):
    # a fresh subscription starts at the tail, the past belongs to the reload
    # path. The connect-time resync then replays FROM the anchor — a no-op
    # when nothing was logged since, and the recovery for the first-window
    # gap (events logged between the anchor read and the registration).
    assert replay.calls[0] == {"session_id": "lore-1", "since_seq": None}
    assert replay.calls[-1] == {"session_id": "lore-1", "since_seq": 7}

    queue, _drop = ch.add_listener("lore-1")
    sock.push(_env("dsh-9", _chunk(7, "stale")))  # seq <= tail anchor: dropped
    sock.push(_env("dsh-9", _chunk(8, "live")))
    got = await _recv(queue)
    assert got["kind"] == "assistant/message" and got["seq"] == 8


@pytest.mark.asyncio
async def test_subscribe_routes_by_envelope_session_id(channel):
    ch, connector, replay, _ = channel
    await ch.subscribe("lore-1")
    await ch.subscribe("lore-2")
    q1, _ = ch.add_listener("lore-1")
    q2, _ = ch.add_listener("lore-2")
    sock = connector.sockets[0]

    sock.push(_env("dsh-9", _chunk(8, "one")))
    sock.push(_env("dsh-2", _chunk(3, "two")))

    a, b = await _recv(q1), await _recv(q2)
    assert a["data"]["message"]["content"][0]["text"] == "one"
    assert b["data"]["message"]["content"][0]["text"] == "two"


# ─── turns through the relay arms ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_flows_through_relay_arms_and_finalizes(channel):
    ch, connector, replay, persists = channel
    await ch.subscribe("lore-1")
    ch.bind_turn("lore-1", assistant_msg_id="m1", user_id="u1")
    queue, _ = ch.add_listener("lore-1")
    sock = connector.sockets[0]

    for frame in (
        {"type": "model_update", "model": "local/test"},
        _chunk(8, "Hello "), _chunk(9, "world"),
        {"type": "context_usage", "used": 5, "cap": 10},
        _turn_end(10, "completed"),
    ):
        sock.push(_env("dsh-9", frame))

    frames = [await _recv(queue) for _ in range(6)]
    assert [f.get("type") for f in frames] == [
        "model_update", "dsh_event", "dsh_event",
        "context_usage", "dsh_event", "done",
    ]
    # The done frame carries the SAME join finalize persists (pump parity).
    assert frames[-1]["content"] == "Hello world"

    await _until(lambda: persists.turn_seq == [("m1", 10)])
    assert persists.content == [("m1", "Hello world")]
    assert persists.context_usage == [("lore-1", 5)]
    assert persists.extras == []  # no halt card on a graceful end
    assert persists.turn_errors == []
    await _until(lambda: ch._subs["lore-1"].turn is None)  # the turn closed


@pytest.mark.asyncio
async def test_a_title_arriving_after_the_turn_closed_still_names_the_chat(channel):
    # The titler runs beside the turn, not inside it: a short answer closes
    # the turn before a slow (reasoning) title model answers, so the LLM title
    # lands AFTER turn/end. It must still reach the chat row and the open
    # client — otherwise the chat keeps the deterministic fallback forever.
    ch, connector, replay, persists = channel
    await ch.subscribe("lore-1")
    ch.bind_turn("lore-1", assistant_msg_id="m1", user_id="u1")
    queue, _ = ch.add_listener("lore-1")
    sock = connector.sockets[0]
    for frame in ({"type": "model_update", "model": "x"}, _chunk(8, "Pong"),
                  _turn_end(9, "completed")):
        sock.push(_env("dsh-9", frame))
    for _ in range(4):
        await _recv(queue)
    await _until(lambda: ch._subs["lore-1"].turn is None)

    sock.push(_env("dsh-9", _dsh(10, "session/title", {"title": "Ping and pong"})))

    assert (await _recv(queue)) == {"type": "session_title", "title": "Ping and pong"}
    assert persists.titles == [("lore-1", "Ping and pong")]


@pytest.mark.asyncio
async def test_unbound_turn_frames_relay_without_persistence(channel):
    ch, connector, replay, persists = channel
    await ch.subscribe("lore-1")
    queue, _ = ch.add_listener("lore-1")
    sock = connector.sockets[0]

    for frame in ({"type": "model_update", "model": "x"}, _chunk(8, "orphan"),
                  _turn_end(9, "completed")):
        sock.push(_env("dsh-9", frame))

    frames = [await _recv(queue) for _ in range(3)]
    assert [f.get("type") for f in frames] == ["model_update", "dsh_event", "dsh_event"]
    assert persists.content == [] and persists.extras == []
    assert persists.turn_seq == [] and persists.turn_errors == []

    # The channel survives the unbound burst: the NEXT bound turn persists.
    ch.bind_turn("lore-1", assistant_msg_id="m2")
    for frame in ({"type": "model_update", "model": "x"}, _chunk(11, "bound"),
                  _turn_end(12, "completed")):
        sock.push(_env("dsh-9", frame))
    for _ in range(3):
        await _recv(queue)
    await _until(lambda: persists.content == [("m2", "bound")])


@pytest.mark.asyncio
async def test_error_frame_without_open_turn_relays_raw(channel):
    ch, connector, replay, persists = channel
    await ch.subscribe("lore-1")
    queue, _ = ch.add_listener("lore-1")
    connector.sockets[0].push(_env("dsh-9", {
        "type": "error", "message": "The turn ended without a terminal event.",
    }))

    got = await _recv(queue)
    assert got["type"] == "error"
    assert persists.extras == [] and persists.turn_errors == []


# ─── resync: reconnect → resubscribe → replay from the last delivered seq ─────


@pytest.mark.asyncio
async def test_resync_replays_from_last_seq_and_continues_the_open_turn(channel):
    ch, connector, replay, persists = channel
    await ch.subscribe("lore-1")  # anchor fetch (tail 7)
    ch.bind_turn("lore-1", assistant_msg_id="m1")
    queue, _ = ch.add_listener("lore-1")
    sock1 = connector.sockets[0]

    for frame in ({"type": "model_update", "model": "x"}, _chunk(8, "A ")):
        sock1.push(_env("dsh-9", frame))
    for _ in range(2):
        await _recv(queue)

    # The WS dies mid-turn. The gap's frames (9, 10) exist only in the log;
    # the replay returns the OPEN turn (no end_seq) and includes the overlap
    # (seq 8, already delivered) that the seq-anchored dedup must drop.
    replay.reply("lore-1", {"turns": [{"frames": [
        _chunk(8, "A "),
        _chunk(9, "B "),
        _chunk(10, "C "),
    ]}], "tail_seq": 10})
    sock1.drop()

    sock2 = await _wait_for_socket(connector, 1)
    assert {"type": "subscribe", "session_id": "lore-1"} in sock2.sent
    assert {"session_id": "lore-1", "since_seq": 8} in replay.calls

    # The gap frames relay in order; the overlap does not re-deliver.
    gap = [await _recv(queue) for _ in range(2)]
    assert [f["data"]["message"]["content"][0]["text"] for f in gap] == ["B ", "C "]

    # Live delivery resumes on the same projection; the terminal finalize
    # persists the WHOLE turn's content — across the gap, joined.
    sock2.push(_env("dsh-9", _chunk(11, "D")))
    sock2.push(_env("dsh-9", _turn_end(12, "completed")))
    tail = [await _recv(queue) for _ in range(3)]
    assert [f.get("type") for f in tail] == ["dsh_event", "dsh_event", "done"]
    assert tail[-1]["content"] == "A B C D"
    await _until(lambda: persists.turn_seq == [("m1", 12)])
    assert persists.content == [("m1", "A B C D")]


# ─── the repoint re-ack: a fork re-keys routing and re-anchors the dedup ──────
# (plan fork-repoints-live-subscription: the driver's /session-leaf repoints
# the live subscription and re-acks with the fork tail; these pin the backend
# half — _handle_subscribed's tail_seq arm.)


@pytest.mark.asyncio
async def test_repoint_reack_rekeys_routing_and_reanchors_dedup(channel):
    ch, connector, replay, _ = channel
    await ch.subscribe("lore-1")
    queue, _ = ch.add_listener("lore-1")
    sock = connector.sockets[0]

    # The pre-fork tail: a frame at seq 100 was delivered and anchored.
    sock.push(_env("dsh-9", _chunk(100, "tail")))
    got = await _recv(queue)
    assert got["seq"] == 100

    # The fork's re-ack: same lore id, FRESH dsh id, tail_seq = the fork's
    # boundary (40 — BELOW the delivered 100: a fork's seed retains the
    # parent prefix seqs). The re-ack rides the socket before the turn's
    # frames (ws send order), and the inbox is FIFO — by the time the seq-41
    # frame is delivered, the re-ack was routed.
    sock.push({
        "type": "subscribed", "session_id": "lore-1",
        "dsh_session_id": "dsh-b", "tail_seq": 40,
    })
    sock.push(_env("dsh-b", _chunk(41, "forked")))
    got = await _recv(queue)
    assert got["seq"] == 41, (
        "seq 41 delivers BELOW the old tail 100 — without the re-anchor the "
        "dedup would drop the whole forked turn as already delivered")

    sub = ch._subs["lore-1"]
    assert sub.dsh_session_id == "dsh-b"
    assert ch._dsh_index.get("dsh-b") is sub
    assert "dsh-9" not in ch._dsh_index, "the pre-fork id no longer routes"
    assert sub.ack.is_set(), (
        "the re-ack re-sets an already-set event — a later ack-once guard "
        "must not break the repoint")

    sock.push(_env("dsh-9", _chunk(42, "orphan")))
    assert await _drain(queue) == [], "a frame addressed by the old id drops"


@pytest.mark.asyncio
async def test_turn_stamps_the_dsh_session_it_ran_in(channel):
    """The row's driver_session is the subscription's dsh id at turn open —
    after a fork's re-ack that is the FRESH session, not the pre-fork one."""
    ch, connector, replay, persists = channel
    await ch.subscribe("lore-1")
    queue, _ = ch.add_listener("lore-1")
    sock = connector.sockets[0]

    ch.bind_turn("lore-1", assistant_msg_id="m1")
    for frame in ({"type": "model_update", "model": "x"}, _chunk(8, "a"),
                  _turn_end(9, "completed")):
        sock.push(_env("dsh-9", frame))
    await _until(lambda: persists.turn_session == [("m1", "dsh-9")])
    await _until(lambda: ch._subs["lore-1"].turn is None)

    sock.push({"type": "subscribed", "session_id": "lore-1",
               "dsh_session_id": "lore-1~ffork0001", "tail_seq": 5})
    ch.bind_turn("lore-1", assistant_msg_id="m2")
    for frame in ({"type": "model_update", "model": "x"}, _chunk(6, "b"),
                  _turn_end(7, "completed")):
        sock.push(_env("lore-1~ffork0001", frame))
    await _until(lambda: persists.turn_session == [
        ("m1", "dsh-9"), ("m2", "lore-1~ffork0001")])
    assert persists.turn_seq == [("m1", 9), ("m2", 7)]


@pytest.mark.asyncio
async def test_repoint_reack_with_null_tail_reanchors_to_nothing(channel):
    # Root fork: tail_seq null — the fresh log is EMPTY, so the dedup anchor
    # resets to "nothing delivered" and the forked turn streams from seq 1.
    ch, connector, replay, _ = channel
    await ch.subscribe("lore-1")
    queue, _ = ch.add_listener("lore-1")
    sock = connector.sockets[0]
    sock.push(_env("dsh-9", _chunk(100, "tail")))
    await _recv(queue)

    sock.push({"type": "subscribed", "session_id": "lore-1",
               "dsh_session_id": "dsh-r", "tail_seq": None})
    sock.push(_env("dsh-r", _chunk(1, "root")))
    got = await _recv(queue)
    assert got["seq"] == 1


@pytest.mark.asyncio
async def test_plain_reack_leaves_the_dedup_anchor_alone(channel):
    ch, connector, replay, _ = channel
    await ch.subscribe("lore-1")
    queue, _ = ch.add_listener("lore-1")
    sock = connector.sockets[0]
    sock.push(_env("dsh-9", _chunk(100, "tail")))
    await _recv(queue)

    # A plain re-ack (no tail_seq — e.g. a reconnect's re-subscribe) re-keys
    # nothing and re-anchors nothing: the seq boundary stays where it was.
    sock.push(_sub_ack("lore-1", "dsh-9"))
    sock.push(_env("dsh-9", _chunk(100, "dup")))
    sock.push(_env("dsh-9", _chunk(101, "next")))
    got = await _recv(queue)
    assert got["seq"] == 101
    assert await _drain(queue) == [], "the stale seq-100 frame stays dropped"
    assert ch._subs["lore-1"].last_seq == 101


# ─── the deadline: silence breach + holds ─────────────────────────────────────


@pytest.mark.asyncio
async def test_silence_breach_halts_persists_and_stops(channel, monkeypatch):
    monkeypatch.setattr(config, "TURN_PROGRESS_GRACE_S", 0.05)
    monkeypatch.setattr(config, "TURN_MAX_WALL_S", 5.0)
    monkeypatch.setattr(config, "TURN_HOLD_MAX_S", 1.0)
    ch, connector, replay, persists = channel
    await ch.subscribe("lore-1")
    ch.bind_turn("lore-1", assistant_msg_id="m1", user_id="u1")
    queue, _ = ch.add_listener("lore-1")
    connector.sockets[0].push(_env("dsh-9", {"type": "model_update", "model": "x"}))
    connector.sockets[0].push(_env("dsh-9", _chunk(8, "partial")))
    for _ in range(2):
        await _recv(queue)

    await asyncio.sleep(0.4)  # >> grace, connected, silent

    err = await _recv(queue)
    assert err["type"] == "error" and err["halt_reason"] == "turn_timeout"
    # The abnormal persist: content + the halt card anchored at the window
    # tail (the pump's TimeoutError branch, no live lore/halt mint).
    await _until(lambda: persists.extras and persists.stops == ["lore-1"])
    assert persists.content == [("m1", "partial")]
    assert persists.extras[0][0] == "m1"
    halt = persists.extras[0][1]["halt"]
    assert halt["reason"] == "turn_timeout" and halt["anchor_seq"] == 8
    assert persists.turn_errors and persists.turn_errors[0]["reason"] == "turn_timeout"
    assert ch._subs["lore-1"].turn is None


@pytest.mark.asyncio
async def test_held_turn_does_not_breach_and_resumes_on_settlement(
        channel, monkeypatch):
    monkeypatch.setattr(config, "TURN_PROGRESS_GRACE_S", 0.05)
    monkeypatch.setattr(config, "TURN_MAX_WALL_S", 5.0)
    monkeypatch.setattr(config, "TURN_HOLD_MAX_S", 5.0)
    ch, connector, replay, _ = channel
    await ch.subscribe("lore-1")
    ch.bind_turn("lore-1", assistant_msg_id="m1")
    queue, _ = ch.add_listener("lore-1")
    sock = connector.sockets[0]
    sock.push(_env("dsh-9", {"type": "model_update", "model": "x"}))
    sock.push(_env("dsh-9", _chunk(8, "held")))
    sock.push(_env("dsh-9", _verdict_ask(8.5)))
    for _ in range(3):
        await _recv(queue)

    await asyncio.sleep(0.3)  # >> grace, but the hold pauses it
    assert queue.empty(), "a held turn must not breach"

    sock.push(_env("dsh-9", _tool_result(9)))
    await _recv(queue)
    await asyncio.sleep(0.4)  # grace after the settled result: breach
    err = await _recv(queue)
    assert err["type"] == "error" and err["halt_reason"] == "turn_timeout"


# ─── unsubscribe: the pump's client-disconnect semantics ─────────────────────


@pytest.mark.asyncio
async def test_unsubscribe_mid_turn_persists_disconnected_and_stops(channel):
    ch, connector, replay, persists = channel
    await ch.subscribe("lore-1")
    ch.bind_turn("lore-1", assistant_msg_id="m1")
    queue, _ = ch.add_listener("lore-1")
    sock = connector.sockets[0]
    sock.push(_env("dsh-9", {"type": "model_update", "model": "x"}))
    sock.push(_env("dsh-9", _chunk(8, "partial")))
    for _ in range(2):
        await _recv(queue)

    await ch.unsubscribe("lore-1")

    assert {"type": "unsubscribe", "session_id": "lore-1"} in sock.sent
    assert persists.extras and persists.extras[0][1]["halt"]["reason"] == "disconnected"
    assert persists.stops == ["lore-1"]
    assert "lore-1" not in ch._subs


# ─── listener close: the None sentinel ────────────────────────────────────────


@pytest.mark.asyncio
async def test_listener_close_sentinel_on_unsubscribe(channel):
    """Unsubscribe closes the session's listeners: the queue yields None
    after its pending frames (plan step 5's pump exits on it)."""
    ch, connector, replay, _ = channel
    await ch.subscribe("lore-1")
    queue, _ = ch.add_listener("lore-1")
    connector.sockets[0].push(_env("dsh-9", _chunk(8, "last")))

    assert (await _recv(queue))["data"]["message"]["content"][0]["text"] == "last"
    await ch.unsubscribe("lore-1")
    assert await _recv(queue) is None


@pytest.mark.asyncio
async def test_listener_close_sentinel_on_bound_drop(channel, monkeypatch):
    """A listener at/past the queue bound is dropped with the None sentinel
    (drop + client resync). Bound 0 drops on the FIRST emit — deterministic:
    the frame never queues, so the consumer's only delivery is the sentinel."""
    ch, connector, replay, _ = channel
    monkeypatch.setattr(driver.channel, "_LISTENER_QUEUE_BOUND", 0)
    await ch.subscribe("lore-1")
    queue, _ = ch.add_listener("lore-1")

    connector.sockets[0].push(_env("dsh-9", _chunk(8, "overflow")))

    assert await _recv(queue) is None
    # The listener is gone from the subscription; the subscription stays.
    assert not ch._subs["lore-1"].listeners


@pytest.mark.asyncio
async def test_aclose_sentinels_listeners(channel):
    """Channel teardown closes every listener — a consumer blocked on
    queue.get() must not be left hanging when the channel dies."""
    ch, connector, replay, _ = channel
    await ch.subscribe("lore-1")
    queue, _ = ch.add_listener("lore-1")

    await ch.aclose()

    assert await _recv(queue) is None


# ─── fetch_session_entries: the since_seq wire param ─────────────────────────


@pytest.mark.asyncio
async def test_fetch_session_entries_since_seq_wire_shape(http_pool):
    import driver.timeline

    log: list[dict] = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"turns": [{"frames": [], "end_seq": 9}], "tail_seq": 9}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, json=None, headers=None, timeout=None):
            log.append({"url": url, "json": json, "headers": headers})
            return _Resp()

    http_pool("driver", _Client())
    line = DriverLine("harness", "http://harness:8090", "s3cr3t")

    out = await driver.timeline.fetch_session_entries(
        "lore-1", line=line, since_seq=8.5)

    assert out == {"turns": [{"frames": [], "end_seq": 9}], "tail_seq": 9}
    assert log[0]["url"] == "http://harness:8090/session-entries"
    assert log[0]["json"] == {"session_id": "lore-1", "since_seq": 8.5}
    assert log[0]["headers"]["X-Driver-Secret"] == "s3cr3t"


@pytest.mark.asyncio
async def test_fetch_session_entries_without_since_seq_omits_it(http_pool, driver_line_pinned):
    import driver.timeline

    log: list[dict] = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"turns": [], "tail_seq": None}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, json=None, headers=None, timeout=None):
            log.append({"url": url, "json": json, "headers": headers})
            return _Resp()

    http_pool("driver", _Client())

    await driver.timeline.fetch_session_entries("lore-1")

    assert log[0]["json"] == {"session_id": "lore-1"}


# ─── helpers ─────────────────────────────────────────────────────────────────


async def _wait_for_socket(connector: FakeConnector, index: int,
                           timeout: float = 2.0) -> FakeSocket:
    deadline = asyncio.get_event_loop().time() + timeout
    while len(connector.sockets) <= index:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"socket {index} never connected")
        await asyncio.sleep(0.01)
    return connector.sockets[index]


# ─── step 6: the turn-end callback (the turn lock's release seam) ─────────────


@pytest.mark.asyncio
async def test_graceful_close_fires_on_end(channel):
    ch, connector, replay, _persists = channel
    await ch.subscribe("lore-1")
    fired: list[int] = []

    async def on_end():
        fired.append(1)

    ch.bind_turn("lore-1", assistant_msg_id="m1", on_end=on_end)
    sock = connector.sockets[0]
    for frame in (
        {"type": "model_update", "model": "local/test"},
        _chunk(8, "Hello "),
        _turn_end(10, "completed"),
    ):
        sock.push(_env("dsh-9", frame))

    await _until(lambda: fired == [1])


@pytest.mark.asyncio
async def test_breach_fires_on_end(channel, monkeypatch):
    ch, connector, replay, _persists = channel
    monkeypatch.setattr(config, "TURN_PROGRESS_GRACE_S", 0.05)
    await ch.subscribe("lore-1")
    fired: list[int] = []

    async def on_end():
        fired.append(1)

    ch.bind_turn("lore-1", assistant_msg_id="m1", on_end=on_end)
    sock = connector.sockets[0]
    sock.push(_env("dsh-9", {"type": "model_update", "model": "local/test"}))
    sock.push(_env("dsh-9", _chunk(8, "Hello ")))

    await _until(lambda: fired == [1])


@pytest.mark.asyncio
async def test_unsubscribe_mid_turn_fires_on_end(channel):
    ch, connector, replay, _persists = channel
    await ch.subscribe("lore-1")
    fired: list[int] = []

    async def on_end():
        fired.append(1)

    ch.bind_turn("lore-1", assistant_msg_id="m1", on_end=on_end)
    sock = connector.sockets[0]
    sock.push(_env("dsh-9", {"type": "model_update", "model": "local/test"}))
    sock.push(_env("dsh-9", _chunk(8, "Hello ")))
    await _until(lambda: ch._subs["lore-1"].turn is not None)

    await ch.unsubscribe("lore-1")
    await _until(lambda: fired == [1])


@pytest.mark.asyncio
async def test_bind_overwrite_fires_the_unclaimed_on_end(channel):
    ch, connector, replay, _persists = channel
    await ch.subscribe("lore-1")
    old_fired: list[int] = []
    new_fired: list[int] = []

    async def old_end():
        old_fired.append(1)

    async def new_end():
        new_fired.append(1)

    ch.bind_turn("lore-1", assistant_msg_id="m1", on_end=old_end)
    ch.bind_turn("lore-1", assistant_msg_id="m2", on_end=new_end)

    await _until(lambda: old_fired == [1])
    assert new_fired == []  # the NEW bind's callback waits for ITS turn


# ─── step 6: emit_frames — the preamble's path into the one writer ────────────


@pytest.mark.asyncio
async def test_emit_frames_reaches_listeners_in_order(channel):
    ch, connector, replay, _persists = channel
    await ch.subscribe("lore-1")
    queue, _ = ch.add_listener("lore-1")

    preamble = [
        {"type": "ids", "user_message_id": "u1", "assistant_message_id": "a1"},
        {"type": "sources", "sources": []},
    ]
    ch.emit_frames("lore-1", preamble)

    got = [await _recv(queue) for _ in range(2)]
    assert got == preamble


@pytest.mark.asyncio
async def test_emit_frames_on_unsubscribed_session_raises(channel):
    ch, _connector, _replay, _persists = channel
    with pytest.raises(RuntimeError):
        ch.emit_frames("lore-none", [{"type": "ids"}])


# ─── step 7: turn_closed — the WS transport's terminal signal ─────────────────
#
# The SSE lifecycle's terminal signal is the STREAM END; a driver-owned turn
# has no stream. The browser closes its streaming slot ONLY on `turn_closed`
# (`done` is a content frame). The PLUGIN pushes turn_closed at its followup
# task's end (after every mapped frame — the halt mint included); the channel
# emits it itself only where the plugin CANNOT: a deadline breach (the driver
# may be dead) and a close whose push is gone — a turn/end lost mid-gap
# (replayed on reconnect) or a turn that ended LIVE before the gap (the
# owed-close re-mint).


@pytest.mark.asyncio
async def test_breach_emits_turn_closed_after_the_error(channel, monkeypatch):
    monkeypatch.setattr(config, "TURN_PROGRESS_GRACE_S", 0.05)
    monkeypatch.setattr(config, "TURN_MAX_WALL_S", 5.0)
    monkeypatch.setattr(config, "TURN_HOLD_MAX_S", 1.0)
    ch, connector, replay, _persists = channel
    await ch.subscribe("lore-1")
    ch.bind_turn("lore-1", assistant_msg_id="m1", user_id="u1")
    queue, _ = ch.add_listener("lore-1")
    connector.sockets[0].push(_env("dsh-9", {"type": "model_update", "model": "x"}))
    await _recv(queue)

    await asyncio.sleep(0.4)  # >> grace, connected, silent

    err = await _recv(queue)
    assert err["type"] == "error" and err["halt_reason"] == "turn_timeout"
    closed = await _recv(queue)
    assert closed == {"type": "turn_closed"}


@pytest.mark.asyncio
async def test_resync_close_of_replayed_turn_emits_turn_closed_after_the_mint(
        channel):
    ch, connector, replay, _persists = channel
    await ch.subscribe("lore-1")
    ch.bind_turn("lore-1", assistant_msg_id="m1")
    queue, _ = ch.add_listener("lore-1")
    sock1 = connector.sockets[0]

    sock1.push(_env("dsh-9", {"type": "model_update", "model": "x"}))
    sock1.push(_env("dsh-9", _chunk(8, "A ")))
    for _ in range(2):
        await _recv(queue)

    # The WS dies mid-turn; the log holds the rest of the turn INCLUDING the
    # plugin's halt mint (the replay returns mapEvent's own output — the mint
    # anchored at the turn/end's seq + the fractional halt offset).
    replay.reply("lore-1", {"turns": [{"frames": [
        _chunk(9, "B"),
        _turn_end(10, "aborted"),
        {"type": "lore/halt", "seq": 10.7, "data": {
            "turn": 1, "reason": "aborted"}, "ignorable": True},
    ]}], "tail_seq": 10})
    sock1.drop()
    await _wait_for_socket(connector, 1)

    frames = [(await _recv(queue)) for _ in range(4)]
    assert [f.get("type") for f in frames] == [
        "dsh_event",        # the gap chunk, relayed into the open projection
        "dsh_event",        # the replayed turn/end — closes the turn
        "lore/halt",        # the replayed mint relays raw (turn already closed)
        "turn_closed",      # the transport terminal, AFTER the turn's frames
    ]


@pytest.mark.asyncio
async def test_resync_remints_turn_closed_for_a_turn_that_ended_live_before_the_gap(
        channel):
    # The lost-push window the replay CANNOT cover: the socket dies AFTER the
    # live turn/end (the listener already saw `done`; _close_turn ran) and
    # BEFORE the plugin's turn_closed push. The push is best-effort and NOT a
    # log entry — the replay is empty, turn/end is deduped by seq. The resync
    # must re-mint the terminal, else the browser hangs on an open turn.
    ch, connector, replay, _persists = channel
    await ch.subscribe("lore-1")
    ch.bind_turn("lore-1", assistant_msg_id="m1")
    queue, _ = ch.add_listener("lore-1")
    sock1 = connector.sockets[0]

    for frame in ({"type": "model_update", "model": "x"}, _chunk(8, "A"),
                  _turn_end(10, "completed")):
        sock1.push(_env("dsh-9", frame))
    # The graceful tail reached the listener (done included) and the turn
    # closed live — the plugin's turn_closed push never arrives.
    frames = [await _recv(queue) for _ in range(4)]
    assert frames[-1]["type"] == "done"
    await _until(lambda: ch._subs["lore-1"].turn is None)

    # The log holds nothing past the delivered tail: the replay is EMPTY.
    replay.reply("lore-1", {"turns": [], "tail_seq": 10})
    sock1.drop()
    await _wait_for_socket(connector, 1)

    closed = await _recv(queue)
    assert closed == {"type": "turn_closed"}


@pytest.mark.asyncio
async def test_no_resync_remint_when_the_plugin_push_was_delivered(channel):
    ch, connector, replay, _persists = channel
    await ch.subscribe("lore-1")
    ch.bind_turn("lore-1", assistant_msg_id="m1")
    queue, _ = ch.add_listener("lore-1")
    sock1 = connector.sockets[0]

    for frame in ({"type": "model_update", "model": "x"}, _chunk(8, "A"),
                  _turn_end(10, "completed")):
        sock1.push(_env("dsh-9", frame))
    for _ in range(4):
        await _recv(queue)
    await _until(lambda: ch._subs["lore-1"].turn is None)
    # The plugin's terminal push IS delivered (unsequenced, relays raw).
    sock1.push(_env("dsh-9", {"type": "turn_closed"}))
    assert (await _recv(queue)) == {"type": "turn_closed"}

    replay.reply("lore-1", {"turns": [], "tail_seq": 10})
    sock1.drop()
    await _wait_for_socket(connector, 1)

    # No second terminal: the resync re-mints only an UNDELIVERED close.
    with pytest.raises(asyncio.TimeoutError):
        await _recv(queue, timeout=0.3)


@pytest.mark.asyncio
async def test_no_resync_remint_while_a_followup_is_already_pending(channel):
    # A followup for the NEXT turn is accepted (bind staked, no first frame
    # yet): the previous turn's re-minted terminal must not close the new
    # turn's browser registration — no mint while a turn is owed.
    ch, connector, replay, _persists = channel
    await ch.subscribe("lore-1")
    fired: list[int] = []

    async def on_end():
        fired.append(1)

    ch.bind_turn("lore-1", assistant_msg_id="m1", on_end=on_end)
    queue, _ = ch.add_listener("lore-1")
    sock1 = connector.sockets[0]
    for frame in ({"type": "model_update", "model": "x"}, _chunk(8, "A"),
                  _turn_end(10, "completed")):
        sock1.push(_env("dsh-9", frame))
    for _ in range(4):
        await _recv(queue)
    await _until(lambda: fired == [1])

    ch.bind_turn("lore-1", assistant_msg_id="m2")  # the next turn, staked
    replay.reply("lore-1", {"turns": [], "tail_seq": 10})
    sock1.drop()
    await _wait_for_socket(connector, 1)

    with pytest.raises(asyncio.TimeoutError):
        await _recv(queue, timeout=0.3)


# ─── _HoldPausedDeadline unit tests (progress-extension semantics) ──────────
# The pure class behind the channel's turn deadline. The channel-level tests
# above cover the silence breach and the held-turn resume; these pin the wall
# clamp, the budget naming and the aggregate pause cap — the properties that
# decide whether an endless-tool-loop turn is ever killed.


class _FakeMonotonic:
    """A controllable clock for the deadline unit tests. driver.channel reads
    time.monotonic() through the shared `time` module; asyncio's event loop
    captured its own bound reference at construction, so the patch is safe to
    hold for the duration of these synchronous tests."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def _deadline(monkeypatch, grace=300.0, wall=1800.0, pause_cap=600.0):
    clock = _FakeMonotonic()
    monkeypatch.setattr(driver.channel.time, "monotonic", clock.monotonic)
    return driver.channel._HoldPausedDeadline(grace, wall, pause_cap), clock


def test_deadline_no_progress_breaches_at_grace(monkeypatch):
    d, clock = _deadline(monkeypatch)
    clock.advance(299.0)
    assert d.remaining() > 0
    clock.advance(2.0)  # past the 300s grace with not a single frame
    assert d.remaining() <= 0


def test_deadline_progress_keeps_a_turn_alive_past_the_flat_total(monkeypatch):
    d, clock = _deadline(monkeypatch)
    # A frame every grace/2 (150s) re-arms the deadline each time: after 750s
    # of steady progress the turn is still alive — a flat 300s total (armed
    # once at turn start, never re-armed) would have breached long before.
    for _ in range(5):
        clock.advance(150.0)
        d.note_progress()
    assert d.remaining() > 0


def test_deadline_ceaseless_progress_breaches_at_the_wall(monkeypatch):
    d, clock = _deadline(monkeypatch)
    for _ in range(11):  # t = 1650: 11 × 150s of steady progress
        clock.advance(150.0)
        d.note_progress()
    assert d.remaining() > 0  # grace extension still buys time under the wall
    clock.advance(150.0)      # t = 1800 = start + TURN_MAX_WALL_S
    d.note_progress()
    assert d.remaining() <= 0  # the extension is clamped at the wall


def test_breached_budget_names_the_silent_window(monkeypatch):
    d, clock = _deadline(monkeypatch)
    clock.advance(301.0)  # not a single frame — silence past the 300s grace
    assert d.remaining() <= 0
    assert d.breached_budget() == "grace"


def test_breached_budget_names_the_wall(monkeypatch):
    d, clock = _deadline(monkeypatch)
    for _ in range(12):  # steady progress clamps the deadline onto the wall
        clock.advance(150.0)
        d.note_progress()
    assert d.remaining() <= 0
    assert d.breached_budget() == "wall"


def test_deadline_a_hold_still_adds_its_grant(monkeypatch):
    d, clock = _deadline(monkeypatch)
    clock.advance(200.0)  # the turn is mid-work at t = 200
    d.pause()             # an approval hold parks the stream
    clock.advance(400.0)  # the user takes 400s to answer
    d.resume()            # grant min(400, cap 600) → deadline 700, wall 2200
    clock.advance(50.0)   # t = 650: real work resumes
    assert d.remaining() > 0  # without the grant the grace (300) breached at t = 300
    # The grant extends the WALL ceiling too: keep producing past t = 1800
    # (the un-paused wall) and the turn is still alive.
    for _ in range(10):   # notes every 150s → t = 2150
        clock.advance(150.0)
        d.note_progress()
    assert d.remaining() > 0
    clock.advance(50.0)   # t = 2200 = start + wall(1800) + grant(400)
    assert d.remaining() <= 0


def test_deadline_pause_cap_is_aggregate_per_turn(monkeypatch):
    d, clock = _deadline(monkeypatch)
    # Two sequential holds of 400s each draw from ONE 600s grant: the second
    # hold is granted only the 200s of headroom left, and the 200s it held
    # beyond the cap burn both budgets.
    d.pause(); clock.advance(400.0); d.resume()   # grant 400 → wall 2200
    d.note_progress()
    d.pause(); clock.advance(400.0); d.resume()   # grant 200 → wall 2400
    for _ in range(10):
        clock.advance(150.0)
        d.note_progress()
    # t = 2300 = start + 800 held + 1500 of work: alive only because the
    # aggregate grant (600) moved the wall to 2400.
    assert d.remaining() > 0
    clock.advance(100.0)  # t = 2400 = start + wall(1800) + cap(600)
    assert d.remaining() <= 0
    # Mid-hold past an exhausted cap: remaining() keeps ticking down.
    d.pause(); clock.advance(10.0)
    assert d.remaining() <= -10.0
