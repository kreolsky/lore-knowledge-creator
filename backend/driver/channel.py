"""Driver event channel — the backend's standing subscription to the
driver's /ws/events endpoint.

Subsystem overview and ARCH notes live in client.py.
See SYSTEM: driver-client (entry: driver/client.py).

The driver owns the turn lifecycle; the backend holds ONE WebSocket to the
driver, subscribes per lore session id, and receives every frame the driver's
standing channel mints — ENVELOPED as ``{type:'session_frame', session_id:<dsh
id>, frame}`` (the frame itself carries no session attribution, so the shared
socket could not route a bare one; the frame inside is mapEvent verbatim).
Received frames flow through the relay arms (frames._relay_frame): no
per-turn HTTP — the accumulation, the persistence and the done/error products
live there.

# ARCH: the channel is the driver-owned lifecycle's backend half. A turn is:
# the turn path POSTs /followup and BINDS the assistant row it created
# (bind_turn); the driver pushes the turn's frames here; the channel's
# per-turn projection persists them. Live/reload parity is by construction —
# the channel relays the same mints, and a reconnect resyncs through the SAME
# projection the reload reads (POST /session-entries with since_seq = the last
# delivered seq; the projection replays the open turn as a ReplayedTurn with
# no end_seq, so a mid-turn gap CONTINUES the live projection rather than
# building a second timeline).

# INVARIANT(resync): live frames are never dispatched past a pending replay.
# Why: on (re)connect the channel subscribes first, buffers any frames that
# arrive before the replay phase completes, replays from each session's last
# delivered seq, and only then dispatches the buffer — anything else would
# let a live frame outrun the gap it belongs after, and the seq-anchored
# dedup below would then DROP the gap's own replay as "already seen",
# silently losing it. The dedup rule itself: a frame with seq <= the
# session's last delivered seq is a replay/live overlap — the mints anchor
# fractional seqs, so the comparison is on floats and both sides of the
# boundary (a mint at 8.5 against last_seq 8.5) agree.

# ARCH: the progress-extended, hold-paused whole-turn deadline
# (`_HoldPausedDeadline`) is the ONLY thing that bounds a silent turn — the
# driver's followup runner runs a turn to its end autonomously and nothing
# driver-side caps it. Every frame re-arms the no-progress budget, a
# verdict-ask hold pauses it, a settled tool/result resumes it, and a WS
# DISCONNECT pauses it too (an unobservable channel must not kill a turn that
# may be alive — the TURN_HOLD_MAX_S grant cap still bounds the total pause,
# so a flapping connection cannot extend a turn forever). A breach is the
# explicit error frame + abnormal persist + turn_timeout telemetry + a
# best-effort POST /stop (feeding the plugin's ONE cancel arm).

Scope boundary (deliberate): the channel keeps its per-session state IN
MEMORY. A backend restart mid-turn loses the open turn's projection — its
frames survive in the driver's log and the row stays empty until the turn is
re-bound by a later followup; the browser adopts an orphaned open turn from
the messages GET's open_turn mark, never from this channel.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import settings

# The persistence writes are read through the OWNING module's attribute at
# call time (never a top-level `from persistence import _persist_content` —
# a frozen binding makes a patch on `driver.persistence._persist_content`
# succeed while inert), so tests string-patch the owner and every consumer
# — this channel and the relay arms alike — sees the fake.
from driver import persistence
from driver.client import DriverLine, resolve_driver_line
from driver.frames import _relay_frame, _result_call_id, _TurnProjection
from driver.timeline import fetch_session_entries, post_stop

logger = logging.getLogger(__name__)

#: Reconnect backoff bounds — the driver is allowed to be down; the channel
#: retries while any subscription stands.
_RECONNECT_MIN_S = 0.5
_RECONNECT_MAX_S = 30.0
#: How long subscribe() waits for the driver's `subscribed` ack before
#: surfacing an explicit failure (the caller decides what that means).
_SUBSCRIBE_ACK_TIMEOUT_S = 10.0
#: Deadline sweep cadence — a breach fires within one tick of its deadline.
_WATCHDOG_TICK_S = 2.0
#: A listener that fell this far behind is dropped (drop + client resync,
#: never reorder) — the browser's own backpressure policy lives in the
#: fan-out (routes.chat.fanout); this bound is the channel's safety net.
_LISTENER_QUEUE_BOUND = 10_000


async def _ws_connect(url: str, headers: dict):
    """The transport seam — open the backend→driver WebSocket.

    Injected in tests (FakeConnector). The real transport is `websockets`
    (the uvicorn[standard] extra, pinned in requirements.txt because this is
    now a direct import): the 14+ asyncio client, `additional_headers`.
    """
    import websockets

    return await websockets.connect(url, additional_headers=headers)


def _events_url(line: DriverLine) -> str:
    """`http(s)://host/ws/events` → `ws(s)://…` — the channel rides the same
    compose-network address the HTTP endpoints use (no host port)."""
    base = line.url
    if base.startswith("https://"):
        return base.replace("https://", "wss://", 1) + "/ws/events"
    if base.startswith("http://"):
        return base.replace("http://", "ws://", 1) + "/ws/events"
    return base + "/ws/events"


class _HoldPausedDeadline:
    """The whole-turn deadline, paused while a mid-turn approval hold awaits
    the user and extended by progress (ARCH: progress-extended hold-paused
    deadline).

    It is the ONLY thing that bounds a silent turn: the driver's followup
    runner runs a turn to its end autonomously and nothing driver-side caps it.

    Two budgets, both measured in non-held wall time and both armed at turn
    start:
    - `no_progress_s` (TURN_PROGRESS_GRACE_S): the silence budget. Armed once
      at start, then re-armed by every real driver frame (`note_progress`) to
      `now + no_progress_s` — a turn that is demonstrably alive is never
      killed by the wall clock; only a silent one dies, one grace window after
      its last frame.
    - `wall_s` (TURN_MAX_WALL_S): the absolute ceiling. The re-arm is clamped
      so the deadline can never pass `now + wall_s` from turn start — even a
      turn that never stops emitting frames breaches at the wall, which is
      what keeps the emergency stop alive under steady progress.

    The pause grant is AGGREGATE per turn and capped at `pause_cap_s`
    (TURN_HOLD_MAX_S): one long hold and several sequential holds draw from the
    SAME grant, so a hold can never outlive the cap. Held time is excluded
    from BOTH budgets — the grant moves the deadline and the wall together — so
    the wall-clock worst case of a turn is `wall_s + pause_cap_s`, never
    unbounded, even when a hold's resolution frame never arrives (past the cap
    both budgets keep ticking and the breach fires).
    """

    def __init__(
        self, no_progress_s: float, wall_s: float, pause_cap_s: float,
    ) -> None:
        now = time.monotonic()
        # The budgets as DURATIONS (public): the breach wording reports the
        # same numbers the deadline enforces — read off the object, never
        # re-resolved from settings (a mid-turn override must not rewrite the
        # wording of a deadline that still runs on the old numbers).
        self.no_progress_s = no_progress_s
        self.wall_s = wall_s
        self.pause_cap_s = pause_cap_s
        self._deadline = now + no_progress_s
        self._wall = now + wall_s
        self._pause_started: float | None = None
        self._pause_granted = 0.0

    def note_progress(self) -> None:
        """A real driver frame arrived: re-arm the silence budget to
        `now + no_progress_s`, clamped at the absolute wall."""
        now = time.monotonic()
        self._deadline = min(
            max(self._deadline, now + self.no_progress_s), self._wall,
        )

    def breached_budget(self) -> str:
        """Which budget fired at a breach: "wall" when steady progress pushed
        the deadline onto the absolute ceiling (a productive runaway — the
        endless-tool-loop class), "grace" when silence outlived the
        no-progress window (a wedged driver). Only meaningful once
        remaining() <= 0."""
        return "wall" if self._deadline >= self._wall else "grace"

    def pause(self) -> None:
        if self._pause_started is None:
            self._pause_started = time.monotonic()

    def resume(self) -> None:
        if self._pause_started is None:
            return
        held = time.monotonic() - self._pause_started
        self._pause_started = None
        grant = min(held, self.pause_cap_s - self._pause_granted)
        if grant > 0:
            self._pause_granted += grant
            self._deadline += grant
            self._wall += grant

    def remaining(self) -> float:
        now = time.monotonic()
        if self._pause_started is None:
            return min(self._deadline, self._wall) - now
        # Mid-hold: the eventual grant is at most the cap headroom; held time
        # beyond that already burns both budgets (the cap is exhausted).
        held = now - self._pause_started
        grant_available = max(0.0, self.pause_cap_s - self._pause_granted)
        return min(self._deadline, self._wall) + min(held, grant_available) - now


@dataclass
class _TurnBind:
    """What completions_turn registers before POSTing /followup: the
    assistant row THIS turn writes (the tool ctx's per-request half),
    the facts its projection persists against, and the turn-END callback.

    # ARCH: on_end is the turn lock's release seam — the caller
    # (routes.chat.completions_harness) holds the per-session lock for the
    # WHOLE driver-owned turn and releases it when the channel closes the
    # turn (graceful end, deadline breach, unsubscribe). The channel knows
    # nothing about locks; it just promises the callback fires ONCE per bind
    # that claimed a turn — and on overwrite of a bind that never claimed one
    # (its followup never produced a first frame; releasing there is correct:
    # the turn it was holding the lock for never started).
    """

    assistant_msg_id: str
    sources: list | None = None
    user_id: str = ""
    on_end: Callable[[], Awaitable[None]] | None = None


@dataclass
class _ChannelTurn:
    """One driver-owned turn's channel state: the relay projection (the same
    _TurnProjection the relay arms advance) plus its deadline machinery, the
    verdict-hold call ids, and the bind's on_end callback."""

    projection: _TurnProjection
    deadline: _HoldPausedDeadline
    user_id: str = ""
    held_calls: set[str] = field(default_factory=set)
    on_end: Callable[[], Awaitable[None]] | None = None


@dataclass
class _Subscription:
    """Per lore session: the driver-side identity, the resync anchor, the
    bound-but-not-started turn, the open turn, and the fan-out listeners."""

    lore_session_id: str
    dsh_session_id: str | None = None
    subscribed: bool = False
    #: The last delivered seq (fractional-aware — the mints anchor halves).
    #: None = nothing delivered yet (fresh anchor on an empty log).
    last_seq: float | None = None
    #: The anchor/replay bookkeeping ran once (fresh subscribe anchored at
    #: the log tail); a reconnect replays since last_seq.
    anchored: bool = False
    pending_bind: _TurnBind | None = None
    turn: _ChannelTurn | None = None
    ack: asyncio.Event = field(default_factory=asyncio.Event)
    listeners: set[asyncio.Queue] = field(default_factory=set)
    #: How many turns _close_turn has closed on this subscription. The resync
    #: replay snapshots it to learn that a REPLAYED frame ended a turn — the
    #: plugin's own turn_closed push for that turn died with the lost socket,
    #: so the channel re-mints the transport terminal itself.
    closes: int = 0


class DriverChannel:
    """The standing backend→driver event channel — one socket, subscribe per
    lore session id, reconnect + resync on loss.

    Lifecycle: `subscribe()` registers a session and anchors its dedup at the
    driver's log tail; the background worker connects (lazily, on the first
    subscription), subscribes, replays any gap, and reads. `bind_turn()`
    binds the next turn's assistant row; `model_update` (the followup's first
    frame) opens the projection; the terminal frames close it (graceful
    finalize + context stamp). Listeners (the fan-out) receive the
    relay arms' output frames in delivery order.
    """

    def __init__(self) -> None:
        self._subs: dict[str, _Subscription] = {}
        self._dsh_index: dict[str, _Subscription] = {}
        self._run_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._socket = None
        self._connected = False
        self._closing = False
        self._wake = asyncio.Event()
        self._send_lock = asyncio.Lock()

    # ── public surface ──────────────────────────────────────────────────────

    async def subscribe(self, lore_session_id: str) -> None:
        """Subscribe to a session's frames — idempotent.

        Anchors the dedup at the driver's CURRENT log tail first (the past
        belongs to the reload path, not to the live channel), then registers
        the subscription and waits for the driver's ack. Raises
        DriverTimelineUnavailable (anchor fetch) or RuntimeError (ack
        timeout) — an unsubscribed session is never left half-registered.

        # INVARIANT: the anchor is fetched BEFORE the subscribe frame is
        # sent. Why: every frame pushed after registration carries a seq
        # appended after the anchor read, so the seq-anchored dedup can never
        # drop a live frame as "already replayed". The cost is a first-window
        # gap — events logged between the anchor read and the registration
        # are not pushed — which any later resync recovers through the same
        # projection (they live in the log).
        """
        if lore_session_id in self._subs:
            return
        payload = await fetch_session_entries(lore_session_id)
        sub = _Subscription(lore_session_id=lore_session_id)
        sub.last_seq = payload.get("tail_seq")
        sub.anchored = True
        self._subs[lore_session_id] = sub
        self._ensure_tasks()
        self._wake.set()
        try:
            await asyncio.wait_for(
                sub.ack.wait(), timeout=_SUBSCRIBE_ACK_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            self._subs.pop(lore_session_id, None)
            raise RuntimeError(
                f"driver channel: no subscribe ack for {lore_session_id} "
                f"within {_SUBSCRIBE_ACK_TIMEOUT_S:g}s"
            ) from exc

    async def unsubscribe(self, lore_session_id: str) -> None:
        """Drop a session's subscription.

        Mid-turn this is the client-disconnect semantics — persist the
        partial product + a `disconnected` halt card (a reload must not show
        a silent empty message) — plus a best-effort POST /stop so the turn
        the backend stopped listening to is cancelled driver-side, not left
        running into an unread log.
        """
        sub = self._subs.pop(lore_session_id, None)
        if sub is None:
            return
        # Close the listeners FIRST (the None sentinel — see add_listener):
        # the fan-out pumps below may block in queue.get() while the disconnect
        # finalize below runs its own awaits.
        for queue in sub.listeners:
            queue.put_nowait(None)
        if sub.dsh_session_id:
            self._dsh_index.pop(sub.dsh_session_id, None)
        sock = self._socket
        if sock is not None and self._connected:
            try:
                await self._send(sock, {
                    "type": "unsubscribe", "session_id": lore_session_id,
                })
            except Exception:
                logger.warning(
                    "driver channel: unsubscribe send failed session=%s",
                    lore_session_id, exc_info=True,
                )
        if sub.turn is not None:
            turn, sub.turn = sub.turn, None
            try:
                await turn.projection.disconnect_finalize()
            except Exception:
                logger.exception(
                    "driver channel: disconnect persist failed session=%s",
                    lore_session_id,
                )
            await self._stop_quietly(lore_session_id)
            if turn.on_end is not None:
                await self._quiet_on_end(turn.on_end)

    def bind_turn(
        self, lore_session_id: str, *, assistant_msg_id: str,
        sources: list | None = None, user_id: str = "",
        on_end: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Bind the NEXT turn on this session to the assistant row it writes.

        Called by the turn path just before POSTing /followup.
        The bind waits unclaimed until the followup's first frame
        (`model_update`) opens the projection; a second bind overwrites an
        unclaimed one (the driver's 409 turn_in_progress serializes turns,
        so an overwrite can only be a retry's newer facts) — and fires the
        overwritten bind's on_end: the turn it was bound to never started, so
        whatever it held (the turn lock) must be released, not leaked to TTL.
        """
        sub = self._subs.get(lore_session_id)
        if sub is None:
            raise RuntimeError(
                f"driver channel: bind_turn on unsubscribed session {lore_session_id}")
        old = sub.pending_bind
        if old is not None and old.on_end is not None:
            asyncio.ensure_future(self._quiet_on_end(old.on_end))
        sub.pending_bind = _TurnBind(
            assistant_msg_id=assistant_msg_id, sources=sources, user_id=user_id,
            on_end=on_end,
        )

    def add_listener(
        self, lore_session_id: str,
    ) -> tuple[asyncio.Queue, Callable[[], None]]:
        """Receive this session's relayed frames (the relay arms' OUTPUT —
        mints, done and error frames included) in delivery order. The remove
        closure detaches.

        WHY(listener-close): the queue yields None as the CLOSE signal —
        bound-breach drop, session unsubscribe, or channel close. The
        consumer (the fan-out pump) parks on get(); without an
        explicit close it would hang past the listener's death while frames
        continue flowing to nobody. Frames queued before the close still
        deliver first (order is never violated by the close); the
        subscription itself may OUTLIVE the listener (a bound-breach drop
        detaches the queue only — a later add_listener re-attaches).
        """
        sub = self._subs.get(lore_session_id)
        if sub is None:
            raise RuntimeError(
                f"driver channel: listener on unsubscribed session {lore_session_id}")
        queue: asyncio.Queue = asyncio.Queue()
        sub.listeners.add(queue)

        def remove() -> None:
            sub.listeners.discard(queue)

        return queue, remove

    def emit_frames(self, lore_session_id: str, frames: list[dict]) -> None:
        """Inject backend-minted frames into the session's listener queues —
        the SAME single writer, in delivery order, as the driver's frames.

        # ARCH: the turn path emits the preamble (ids/sources/context_warning)
        # and the pre-followup failure frames
        # (error/done) HERE, BEFORE POSTing /followup — the listener queue is
        # the one ordering point, so the browser can never see a driver frame
        # ahead of the preamble it belongs after. The dict shapes are the WS
        # frame vocabulary the relay arms speak.
        #
        # Raises RuntimeError on an unsubscribed session: losing a preamble
        # silently would leave the browser's turn unrendered — a caller bug,
        # not a drop condition.
        """
        sub = self._subs.get(lore_session_id)
        if sub is None:
            raise RuntimeError(
                f"driver channel: emit_frames on unsubscribed session "
                f"{lore_session_id}"
            )
        self._emit(sub, list(frames))

    async def _quiet_on_end(
        self, on_end: Callable[[], Awaitable[None]],
    ) -> None:
        """Await a turn-end callback, logging instead of raising — the
        callback releases a lock and must never break the channel's own
        close path."""
        try:
            await on_end()
        except Exception:
            logger.exception("driver channel: turn on_end callback failed")

    async def aclose(self) -> None:
        """Tear the channel down (tests, process shutdown)."""
        self._closing = True
        self._wake.set()
        tasks = [t for t in (self._run_task, self._watchdog_task) if t is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Close every listener (the None sentinel) AFTER the workers stop —
        # a consumer parked on get() must not outlive the channel.
        for sub in self._subs.values():
            for queue in sub.listeners:
                queue.put_nowait(None)
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                await sock.close()
            except Exception:
                pass

    # ── the connection worker ───────────────────────────────────────────────

    def _ensure_tasks(self) -> None:
        if self._closing:
            return
        if self._run_task is None or self._run_task.done():
            self._run_task = asyncio.ensure_future(self._run())
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.ensure_future(self._watchdog())

    async def _run(self) -> None:
        backoff = _RECONNECT_MIN_S
        while not self._closing and self._subs:
            line = await resolve_driver_line()
            if line is None:
                logger.warning(
                    "driver channel: line unconfigured; subscription idle")
                await self._backoff_sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_MAX_S)
                continue
            try:
                sock = await _ws_connect(
                    _events_url(line), {"X-Driver-Secret": line.secret})
            except Exception as exc:
                logger.warning(
                    "driver channel: connect failed (%s); retrying", exc)
                await self._backoff_sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_MAX_S)
                continue
            try:
                await self._on_connection(line, sock)
                backoff = _RECONNECT_MIN_S
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("driver channel: connection ended (%s)", exc)
            finally:
                self._set_connected(False)
                try:
                    await sock.close()
                except Exception:
                    pass
                self._socket = None
            await self._backoff_sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX_S)

    async def _on_connection(self, line: DriverLine, sock) -> None:
        """One socket's lifetime: subscribe everything, resync the gaps,
        then read — the resync INVARIANT's ordering lives here."""
        self._socket = sock
        buffered = await self._subscribe_phase(line, sock)
        await self._resync_all(line)
        for raw in buffered:
            await self._route(raw)
        self._set_connected(True)
        logger.info(
            "driver channel: connected, %d session(s) subscribed", len(self._subs))

        recv_task = asyncio.ensure_future(sock.recv())
        wake_task = asyncio.ensure_future(self._wake.wait())
        try:
            while True:
                await asyncio.wait(
                    {recv_task, wake_task}, return_when=asyncio.FIRST_COMPLETED)
                fired_wake = wake_task.done()
                if fired_wake:
                    self._wake.clear()
                    wake_task = asyncio.ensure_future(self._wake.wait())
                if recv_task.done():
                    # Exceptions propagate: the socket died (reconnect).
                    raw = recv_task.result()
                    recv_task = asyncio.ensure_future(sock.recv())
                    await self._route(raw)
                if fired_wake:
                    await self._send_new_subscribes(sock)
        finally:
            recv_task.cancel()
            wake_task.cancel()

    async def _subscribe_phase(self, line: DriverLine, sock) -> list[str]:
        """(Re)subscribe every standing session on THIS socket, awaiting each
        ack inline. Frames that arrive while acks are pending are BUFFERED —
        the replay must apply first (the resync INVARIANT)."""
        del line  # the socket is already authorized; the line is resync's
        pending: set[str] = set()
        for sub in list(self._subs.values()):
            try:
                await self._send(sock, {
                    "type": "subscribe", "session_id": sub.lore_session_id,
                })
            except Exception as exc:
                raise ConnectionError(f"subscribe send failed: {exc}") from exc
            pending.add(sub.lore_session_id)
        buffered: list[str] = []
        deadline = asyncio.get_event_loop().time() + _SUBSCRIBE_ACK_TIMEOUT_S
        while pending:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise ConnectionError(
                    f"subscribe ack timeout: {sorted(pending)}")
            try:
                raw = await asyncio.wait_for(sock.recv(), remaining)
            except asyncio.TimeoutError as exc:
                raise ConnectionError(
                    f"subscribe ack timeout: {sorted(pending)}") from exc
            msg = _loads(raw)
            if (isinstance(msg, dict) and msg.get("type") == "subscribed"
                    and msg.get("session_id") in pending):
                self._handle_subscribed(msg)
                pending.discard(str(msg.get("session_id")))
            else:
                buffered.append(raw)
        return buffered

    async def _resync_all(self, line: DriverLine) -> None:
        """Replay every session's gap through the SAME dispatch the live
        frames take — since each sub's last delivered seq (the projection's
        since_seq). A failed replay is logged and skipped: the
        frames live in the driver's log and the next reconnect retries; the
        connection itself must not die over one session's read.

        # WHY: a driver CRASH (SIGKILL mid-turn) is closed by THIS path, not
        # by the 300s no-progress grace: dsh's cold inspect synthesizes the
        # interrupted turn's closers (`turn/end reason:'interrupted'` + the
        # plugin's halt mint) into the replay, so the turn/end arm persists
        # the halt and releases the lock as soon as the channel reconnects
        # (measured on gray: 6–11s after the kill, bounded by the container
        # restart + reconnect backoff). The `[lore-skills] restore failed …
        # not found` line the driver logs on a session's FIRST turn is the
        # skill-activation restore, not a lost session.
        """
        for sub in list(self._subs.values()):
            if not sub.anchored or sub.last_seq is None:
                continue
            closes_before = sub.closes
            try:
                payload = await fetch_session_entries(
                    sub.lore_session_id, line=line, since_seq=sub.last_seq)
            except Exception:
                logger.warning(
                    "driver channel: resync replay failed session=%s "
                    "(retrying on next reconnect)", sub.lore_session_id,
                    exc_info=True,
                )
                continue
            for turn in payload.get("turns") or []:
                for frame in turn.get("frames") or []:
                    if isinstance(frame, dict):
                        await self._dispatch_frame(sub, frame)
            if sub.closes > closes_before:
                # A REPLAYED frame ended a turn whose plugin-side turn_closed
                # push died with the lost socket (the plugin pushed it during
                # the gap, into a socket that never delivered). Emitted AFTER
                # the whole replay, so the turn's trailing frames — the halt
                # mint — reach the listener ahead of the terminal (the same
                # ordering the plugin's own push guarantees live).
                self._emit(sub, [{"type": "turn_closed"}])

    async def _send_new_subscribes(self, sock) -> None:
        """Subscriptions registered while connected: send their subscribe
        (the ack arrives through the normal routing)."""
        for sub in list(self._subs.values()):
            if sub.subscribed:
                continue
            try:
                await self._send(sock, {
                    "type": "subscribe", "session_id": sub.lore_session_id,
                })
            except Exception:
                logger.warning(
                    "driver channel: late subscribe send failed session=%s",
                    sub.lore_session_id, exc_info=True,
                )
                return  # the socket is dying; the reconnect path re-subscribes

    # ── routing and dispatch ────────────────────────────────────────────────

    def _handle_subscribed(self, msg: dict) -> None:
        session_id = str(msg.get("session_id") or "")
        sub = self._subs.get(session_id)
        if sub is None:
            return  # unsubscribed while the ack was in flight
        dsh_id = msg.get("dsh_session_id")
        if sub.dsh_session_id and sub.dsh_session_id in self._dsh_index:
            self._dsh_index.pop(sub.dsh_session_id, None)
        sub.dsh_session_id = str(dsh_id) if dsh_id else None
        if sub.dsh_session_id:
            self._dsh_index[sub.dsh_session_id] = sub
        # WHY(fork re-anchor): a REPOINT re-ack carries `tail_seq` — the
        # forked session's log tail (null on a root fork); a plain subscribe
        # ack never does. The fork's seed RETAINS the parent prefix seqs, so
        # the forked turn's frames start BELOW the pre-fork tail whenever the
        # branch point is not the last turn — without re-anchoring here, the
        # seq-anchored dedup in _dispatch_frame drops the whole forked turn
        # as "already delivered" while the driver completes it happily (the
        # driver repoints the subscription; this arm follows it).
        if "tail_seq" in msg:
            tail = msg.get("tail_seq")
            sub.last_seq = (
                float(tail)
                if isinstance(tail, (int, float)) and not isinstance(tail, bool)
                else None
            )
        sub.subscribed = True
        sub.ack.set()

    async def _route(self, raw: str) -> None:
        msg = _loads(raw)
        if not isinstance(msg, dict):
            logger.warning("driver channel: non-dict frame dropped: %r", raw)
            return
        mtype = msg.get("type")
        if mtype == "subscribed":
            self._handle_subscribed(msg)
            return
        if mtype == "unsubscribed":
            return  # our own unsubscribe's ack — state already dropped
        if mtype == "error":
            # Socket-level protocol error (malformed client frame and the
            # like) — never a turn error: turn errors ride session_frame.
            logger.warning(
                "driver channel: protocol error frame: %s", msg.get("message"))
            return
        if mtype == "session_frame":
            sub = self._dsh_index.get(str(msg.get("session_id") or ""))
            if sub is None:
                logger.debug(
                    "driver channel: frame for unrouted session %s dropped",
                    msg.get("session_id"),
                )
                return
            frame = msg.get("frame")
            if isinstance(frame, dict):
                await self._dispatch_frame(sub, frame)
            return
        logger.warning("driver channel: unknown control frame dropped: %r", msg)

    async def _dispatch_frame(self, sub: _Subscription, frame: dict) -> None:
        """One frame into the session's dispatch: dedup, open the turn on the
        followup's first frame, then progress, holds, the relay arms, the
        terminal close."""
        seq = frame.get("seq")
        if isinstance(seq, (int, float)) and not isinstance(seq, bool):
            if sub.last_seq is not None and seq <= sub.last_seq:
                return  # replay/live overlap — already delivered
            sub.last_seq = seq
        if sub.turn is None:
            etype = frame.get("type")
            if sub.pending_bind is not None and (
                    etype == "model_update"
                    or (etype == "dsh_event" and frame.get("kind") == "turn/start")):
                sub.turn = await self._open_turn(sub)
            elif etype == "dsh_event" and frame.get("kind") == "turn/start":
                # A turn nobody bound (backend restarted mid-turn, or a turn
                # that started inside a resync gap before any bind): relay
                # without persistence — the row binding does not exist, and
                # inventing one would write to a message that is not ours.
                logger.warning(
                    "driver channel: unclaimed turn/start session=%s — "
                    "frames relay without persistence", sub.lore_session_id)
        if sub.turn is None:
            self._emit(sub, [frame])
            return
        turn = sub.turn
        turn.deadline.note_progress()
        etype = frame.get("type")
        if etype == "lore/verdict-ask":
            cid = (frame.get("data") or {}).get("callId") or ""
            if cid:
                turn.held_calls.add(cid)
        elif etype == "dsh_event" and frame.get("kind") == "tool/result":
            turn.held_calls.discard(
                _result_call_id(frame.get("data") or {}))
        self._update_deadline_pause(sub)
        out = await _relay_frame(turn.projection, frame)
        self._emit(sub, out)
        if turn.projection.finished:
            await self._close_turn(sub)

    async def _open_turn(self, sub: _Subscription) -> _ChannelTurn:
        bind, sub.pending_bind = sub.pending_bind, None
        # The turn budgets resolve per turn through instance settings. The
        # grace is a CHAIN read: TURN_PROGRESS_GRACE_S folds
        # TURN_TIMEOUT_S at import, so an override on the base must surface
        # here (settings.get walks the registry fallback: row → own env → base).
        budgets = await settings.get_all(["TURN_MAX_WALL_S", "TURN_HOLD_MAX_S"])
        turn = _ChannelTurn(
            projection=_TurnProjection(
                assistant_msg_id=bind.assistant_msg_id,
                sources=bind.sources,
                persist_content=persistence._persist_content,
                persist_sources=persistence._persist_sources,
                persist_extras=persistence._persist_projection_extras,
                persist_turn_seq=persistence._persist_turn_seq,
                session_id=sub.lore_session_id,
                # WHY: the turn opens on its first frame, which was routed here
                # through _dsh_index by its dsh id — a fork's repoint re-ack
                # rides the same socket ahead of it, so this is the session
                # the turn runs in.
                dsh_session_id=sub.dsh_session_id,
            ),
            deadline=_HoldPausedDeadline(
                no_progress_s=await settings.get("TURN_PROGRESS_GRACE_S"),
                wall_s=budgets["TURN_MAX_WALL_S"],
                pause_cap_s=budgets["TURN_HOLD_MAX_S"],
            ),
            user_id=bind.user_id,
            on_end=bind.on_end,
        )
        self._update_deadline_pause(sub)
        return turn

    async def _close_turn(self, sub: _Subscription) -> None:
        """The terminal close for a turn that
        ENDED (graceful finalize + context stamp, telemetry on an errored
        turn; abnormal persists already ran inside the arms).

        # ARCH: this emits NO turn_closed of its own — the turn
        # was closed by a LIVE terminal frame, and the plugin's followup task
        # pushes turn_closed after every mapped frame of that same event (the
        # halt mint included). An emit here would race AHEAD of that mint and
        # make the browser drop the card. The replay path re-mints the close
        # where the plugin's push is gone (see _resync_all)."""
        turn, sub.turn = sub.turn, None
        if turn is None:
            return
        sub.closes += 1
        projection = turn.projection
        if projection.finalize_pending:
            try:
                await projection.finalize()
            except Exception:
                logger.exception(
                    "driver channel: turn finalize failed session=%s msg=%s",
                    sub.lore_session_id, projection.assistant_msg_id,
                )
            if projection.context_usage:
                try:
                    await persistence._persist_context_usage(
                        sub.lore_session_id,
                        int(projection.context_usage["used"]),
                    )
                except Exception:
                    logger.warning(
                        "driver channel: context_usage persist failed session=%s",
                        sub.lore_session_id, exc_info=True,
                    )
        if projection.errored:
            await self._record_error_quietly(turn, "error_event", "error_event")
        if turn.on_end is not None:
            await self._quiet_on_end(turn.on_end)

    # ── the deadline watchdog ───────────────────────────────────────────────

    async def _watchdog(self) -> None:
        while not self._closing:
            await asyncio.sleep(_WATCHDOG_TICK_S)
            for sub in list(self._subs.values()):
                turn = sub.turn
                if turn is None or turn.deadline.remaining() > 0:
                    continue
                await self._breach(sub, turn)

    async def _breach(self, sub: _Subscription, turn: _ChannelTurn) -> None:
        """The deadline breach: an explicit error frame, the
        abnormal persist (content + halt card anchored at the window tail),
        turn_timeout telemetry, and a best-effort /stop so a turn the channel
        declared dead is cancelled driver-side too. No live lore/halt mint —
        the reload mints it from the row's halt column."""
        sub.turn = None
        projection = turn.projection
        projection.finalize_pending = False
        if turn.deadline.breached_budget() == "wall":
            note = (
                f"Agent turn timed out — it hit the {turn.deadline.wall_s:g}s hard "
                "ceiling (excluding time held for approval)"
            )
        else:
            note = (
                "Agent turn timed out — no driver frames for over "
                f"{turn.deadline.no_progress_s:g}s (excluding time held for approval)"
            )
        logger.warning(
            "driver channel: turn deadline breached budget=%s msg=%s session=%s",
            turn.deadline.breached_budget(),
            projection.assistant_msg_id, sub.lore_session_id,
        )
        self._emit(sub, [{
            "type": "error", "message": note, "halt_reason": "turn_timeout",
        }])
        try:
            await projection.abnormal_finalize("turn_timeout", note)
        except Exception:
            logger.exception(
                "driver channel: breach persist failed session=%s",
                sub.lore_session_id,
            )
        await self._record_error_quietly(turn, "turn_timeout", "turn_timeout")
        await self._stop_quietly(sub.lore_session_id)
        if turn.on_end is not None:
            await self._quiet_on_end(turn.on_end)
        # The transport terminal: a breach ends the turn
        # BACKEND-side and the driver may be exactly what is dead — the
        # plugin's own turn_closed push cannot be relied on here. The browser
        # closes its streaming slot on this frame (nothing follows it: the
        # breach mints no live halt — the reload mints it from the row).
        self._emit(sub, [{"type": "turn_closed"}])

    async def _record_error_quietly(
        self, turn: _ChannelTurn, reason: str, source: str,
    ) -> None:
        try:
            await persistence._record_turn_error(
                assistant_msg_id=turn.projection.assistant_msg_id,
                session_id=turn.projection.session_id,
                reason=reason, source=source,
                user_id=turn.user_id, project_id="",
            )
        except Exception:
            logger.warning(
                "driver channel: turn-error telemetry failed", exc_info=True)

    async def _stop_quietly(self, lore_session_id: str) -> None:
        try:
            await post_stop(lore_session_id)
        except Exception as exc:
            logger.warning(
                "driver channel: POST /stop failed session=%s: %s",
                lore_session_id, exc,
            )

    # ── small pieces ────────────────────────────────────────────────────────

    def _set_connected(self, flag: bool) -> None:
        """Connected-state transitions re-evaluate every open turn's deadline
        pause — a disconnect is an UNOBSERVABLE channel, not a dead turn (the
        grant cap still bounds the total pause)."""
        self._connected = flag
        for sub in self._subs.values():
            self._update_deadline_pause(sub)

    def _update_deadline_pause(self, sub: _Subscription) -> None:
        turn = sub.turn
        if turn is None:
            return
        if not self._connected or turn.held_calls:
            turn.deadline.pause()
        else:
            turn.deadline.resume()

    async def _send(self, sock, msg: dict) -> None:
        async with self._send_lock:
            await sock.send(json.dumps(msg))

    async def _backoff_sleep(self, seconds: float) -> None:
        """Backoff that a new subscription can interrupt (subscribe while the
        driver is down must not wait out the whole backoff)."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
            self._wake.clear()
        except asyncio.TimeoutError:
            pass

    def _emit(self, sub: _Subscription, frames: list[dict]) -> None:
        for queue in list(sub.listeners):
            if queue.qsize() >= _LISTENER_QUEUE_BOUND:
                sub.listeners.discard(queue)
                # The close sentinel (add_listener's WHY(listener-close)): the
                # dropped consumer reads its queued frames, then None, then
                # exits — drop + client resync, never an undetected silent
                # death.
                queue.put_nowait(None)
                logger.warning(
                    "driver channel: listener past the queue bound dropped "
                    "(session=%s; it resyncs from its last delivered seq)",
                    sub.lore_session_id,
                )
                continue
            for frame in frames:
                queue.put_nowait(frame)


def _loads(raw: str) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return None


_channel: DriverChannel | None = None


def get_driver_channel() -> DriverChannel:
    """The process-wide channel (lazily built — a deployment with no harness
    sessions opens no socket)."""
    global _channel
    if _channel is None:
        _channel = DriverChannel()
    return _channel
