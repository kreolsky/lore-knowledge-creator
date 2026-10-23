"""Timeline attach for chat-message reads: stamp the driver's replayed log
onto the assistant rows, mark the open/running turn, read the queue.

Part of SYSTEM: chat-messages (See SYSTEM: chat-messages) — the reload path's
timeline half. No SYSTEM marker
(internal extraction; the chat-messages entry stays in messages.py). The module
reaches the harness (driver.timeline, driver.frames), so it stays
route-resident under routes/chat/ — backend/chat_sessions/__init__.py keeps
harness-reaching code route-resident; the routes→routes import of
routes.chat.fanout is legal.
"""
import logging

from fastapi import HTTPException

from routes.chat.fanout import ensure_fanout

logger = logging.getLogger(__name__)


# ARCH: the TRANSCRIPT is the driver's session log, not a Lore column. The Lore row owns
# what is ours — the id, the author, created_at, the sources panel, the
# images, and the abnormal-end halt card — and the ordered
# timeline (text, reasoning, tool chips, the neutral dsh chips) rides here as
# `frames`, replayed by the plugin through the same mapEvent the live stream
# used. The frontend reduces the live stream and these frames with ONE
# reducer, so a reloaded turn and the streamed turn agree by construction.
#
# INVARIANT(corruption): a row receives the frames of the turn its OWN
# `driver_seq` names, never another turn's. Why: a session's rows resolve
# only against the session's OWN log (dsh id = Lore id; a seeded branch's
# log carries the copied prefix under the SAME seqs), so the per-session stamp map below is the whole
# pairing (an unseeded branch reads its seed source's log instead — see
# _fetch_session_turns). Rows whose stamp does not resolve (a pre-harness
# turn, an abnormally ended turn) keep no `frames` key and the
# client renders what the row carries.
def _assign_frames_by_stamp(assistants: list[dict], turns: list[dict]) -> None:
    """Key each assistant row to its turn by the driver's own id — the row's
    `driver_seq` over the projection's per-turn `end_seq`, within THIS
    session's own log (the per-session stamp map)."""
    by_seq: dict[int, dict] = {}
    for t in turns:
        s = t.get("end_seq")
        if isinstance(s, int) and not isinstance(s, bool):
            by_seq[s] = t
    for r in assistants:
        t = by_seq.get(r.get("driver_seq"))
        if t is not None:
            # The replay of a NON-live session ends every closed turn with a
            # seq-anchored `turn_closed` (plugin entries.ts `terminals`) —
            # TRANSPORT state for the resync consumer (the channel's dispatch
            # arm), never timeline content: the rows' frames are the
            # assembler's input, so it is dropped here.
            r["frames"] = [
                f for f in t.get("frames") or []
                if not (isinstance(f, dict) and f.get("type") == "turn_closed")
            ]


def _log_shows_open_turn(turns: list[dict]) -> bool:
    """Whether the projection's trailing turn is still open (no `end_seq`)."""
    return bool(turns) and isinstance(turns[-1], dict) and turns[-1].get("end_seq") is None


def _mark_open_turn(
    assistants: list[dict], turns: list[dict], *, running: bool,
) -> None:
    """Attach the OPEN turn (the projection's trailing turn with no `end_seq`)
    to the row that turn is writing, flagged
    `open_turn`: the reload's streaming re-seat input (a reload mid-turn
    renders the open turn as STREAMING, never a settled partial row that
    flips on the next live frame). The open turn's `assistant_stream` (the
    plugin's live-stream fold — the streamed text the log does not hold yet)
    passes through VERBATIM beside the mark: the browser seats the transient
    tail from it. Only the trailing OPEN turn's field is read — a closed
    turn never carries one (entries.ts attaches it to the open turn only).

    # INVARIANT(replay-split, corruption, read side): a row that carries `halt`
    # never takes the open mark. Why: the plugin's read closes a dead turn's
    # log with dsh's closers, so a trailing open turn marks a REGISTERED turn —
    # and the one dead shape left is the breach whose /stop failed while the
    # plugin turn still runs (halt card written, session live). Beside that
    # card the open frames would render TWO records for one turn; the halt
    # column is the splitter, mirroring entries.ts's emitting branch.
    """
    if not _log_shows_open_turn(turns):
        if running:
            _mark_running_turn(assistants)
        return
    open_frames = [
        f for f in turns[-1].get("frames") or [] if isinstance(f, dict)
    ]
    candidates = [
        r for r in assistants
        if r.get("halt") is None
        and r.get("driver_seq") is None
        and r.get("frames") is None
    ]
    if not candidates:
        # The trailing turn's row does not exist here (a stamped row already
        # claimed the head, or the open turn is unclaimed — a backend restart
        # mid-turn): nothing claims the open turn this read.
        return
    newest = max(candidates, key=lambda r: r.get("created_at") or "")
    newest["frames"] = list(open_frames)
    newest["open_turn"] = True
    baseline = turns[-1].get("assistant_stream")
    if isinstance(baseline, dict):
        newest["assistant_stream"] = baseline


def _mark_running_turn(assistants: list[dict]) -> None:
    """Mark the running turn the log cannot show yet: the NEWEST assistant row,
    only if it is the open turn's shape (unstamped, no halt, no frames).
    No frames and no `assistant_stream` — nothing streamed is
    logged yet."""
    newest = max(assistants, key=lambda r: r.get("created_at") or "")
    # INVARIANT(corruption): a running dsh agent marks the open turn even when
    # the driver log shows none. Why: between dsh's claim and its log flush
    # the timeline cannot show the turn, and an unmarked reload makes the
    # browser close a live turn as 'lost'. The newest
    # row only — an older abnormally-ended unstamped row is never marked; a
    # `halt` row never is (the halt INVARIANT above keeps priority).
    if (
        newest.get("halt") is None
        and newest.get("driver_seq") is None
        and newest.get("frames") is None
    ):
        newest["frames"] = []
        newest["open_turn"] = True


async def _fetch_session_turns(session: dict, session_id: str) -> dict:
    """The driver's replayed session for this thread's log — the plugin's
    ReplayedSession `{turns, tail_seq}`.

    The turns of a compaction continuation run under the SOURCE session's log
    (`completions.py`: `compacted_from or session_id`), so the read names the
    same lineage — asking for the continuation's own chat id yields an empty
    turn list for an id the driver has never seen, and the rows never get
    frames. The returned id is also the mint identity the reload lore mints
    key on (the compaction window's source side).
    """
    from driver.timeline import DriverTimelineUnavailable, fetch_session_entries
    lineage = session.get("compacted_from") or session_id
    seed_seq = session.get("seed_source_seq")
    seeded_from = (
        session.get("seed_source_session")
        if isinstance(seed_seq, int) and not isinstance(seed_seq, bool) else None
    )
    if seeded_from:
        # INVARIANT(corruption): an unseeded branch reads its SEED SOURCE log.
        # Why: the copies keep the source's seqs, so the source's turns up to
        # the boundary ARE the prefix's frames; the branch's own (empty) log
        # renders the prefix text-only — every chip gone until a first turn
        # succeeds. The read is cut at the boundary (_cut_at_seed) so the
        # source's later turns or live open turn never graft onto the branch.
        lineage = seeded_from
    try:
        replay = await fetch_session_entries(lineage)
    except DriverTimelineUnavailable as exc:
        # No silent degradation: an unreadable transcript is an explicit
        # failure, never an empty thread (DriverTimelineUnavailable's
        # INVARIANT names why).
        logger.warning("agent timeline unavailable session=%s: %s", session_id, exc)
        raise HTTPException(
            status_code=502, detail="The agent timeline could not be read",
        ) from exc
    if seeded_from:
        replay = _cut_at_seed(replay or {}, seed_seq)
    return {"lineage": lineage, **(replay or {})}


async def _read_session_status(session: dict, session_id: str) -> dict:
    """The live dsh agent's `{status, queue, seq}` for this chat's DRIVER
    session (compacted_from or the chat id — the session its sends queue in).
    An unreadable status is a 502, the same arm as an unreadable timeline:
    the reload's open-turn mark and queue derive from it."""
    from driver.timeline import DriverTimelineUnavailable, fetch_session_status
    driver_session_id = session.get("compacted_from") or session_id
    try:
        status = await fetch_session_status(driver_session_id)
    except DriverTimelineUnavailable as exc:
        logger.warning("agent status unavailable session=%s: %s", session_id, exc)
        raise HTTPException(
            status_code=502, detail="The agent timeline could not be read",
        ) from exc
    if status["status"] == "running" or status["queue"]:
        await _ensure_fanout_quietly(session_id, driver_session_id)
    return status


async def _ensure_fanout_quietly(session_id: str, driver_session_id: str) -> None:
    """Re-attach the fan-out for a session whose turns run or wait. WHY: the
    subscription lives in memory and only a completion opened it, so after a
    backend restart the queue's next claims would reach no listener and get
    no rows. Best-effort: the read itself must not fail over it — the next
    send opens it anyway."""
    try:
        await ensure_fanout(session_id, driver_session_id=driver_session_id)
    except Exception:
        logger.warning(
            "fan-out re-attach failed session=%s", session_id, exc_info=True)


def _cut_at_seed(replay: dict, seed_seq: int) -> dict:
    """The seed source's replay as the unseeded branch sees it: closed turns
    ending at or before the boundary only (an open turn is the source's own
    live turn, never this branch's), the tail capped at the boundary."""
    turns = [
        t for t in replay.get("turns") or []
        if isinstance(t, dict) and isinstance(t.get("end_seq"), int)
        and not isinstance(t.get("end_seq"), bool) and t["end_seq"] <= seed_seq
    ]
    tail = replay.get("tail_seq")
    if isinstance(tail, int) and tail > seed_seq:
        tail = seed_seq
    return {**replay, "turns": turns, "tail_seq": tail}


async def _attach_timeline(
    session: dict, session_id: str, out: list[dict], *, offset: int,
) -> dict:
    """Stamp each assistant row with the driver's frames for its turn, then
    mint the reload-side lore events onto the rows. Returns the chat's queue
    — `{queue, queue_seq}` from the live dsh agent, read also for a chat with
    no rows yet (a first message may be queued).

    A note thread never runs a turn — no driver read. A thread with no
    assistant row reads only the status (its first message may be queued).
    Rows the log has no turn for (threads predating
    the harness, an abnormally ended turn that never wrote `turn/end`) keep the Lore row exactly as it is: no `frames`
    key from the replay — and the reload mints add exactly what the row owns
    (the `halt` column's card at the log tail; a gen_steps image run whose
    dispatching call the replay carries). The client feeds the rows' frames
    to the assembler, so the mints ride the same input as the replay.
    """
    if session.get("is_note") is True:
        return {"queue": [], "queue_seq": None}
    assistants = [m for m in out if m.get("role") == "assistant"]
    if assistants and offset:
        # The page starts mid-thread, so the page's rows alone cannot anchor
        # the pairing. Refusing is the honest answer — a silently
        # mis-aligned timeline would show one turn's tool calls under
        # another's answer.
        raise HTTPException(
            status_code=400,
            detail="Paginated message reads cannot resolve the agent timeline; use offset=0",
        )
    if not assistants:
        status = await _read_session_status(session, session_id)
        return {"queue": status["queue"], "queue_seq": status["seq"]}
    replay = await _fetch_session_turns(session, session_id)
    turns = replay.get("turns") or []
    tail_seq = replay.get("tail_seq")
    logged = bool(turns) or tail_seq is not None
    if logged:
        _assign_frames_by_stamp(assistants, turns)
    status = await _read_session_status(session, session_id)
    # The open turn: AFTER the stamp pass — its row is by definition
    # unstamped, so the two never contend for one row.
    _mark_open_turn(assistants, turns, running=status["status"] == "running")
    if logged:
        from driver.frames import attach_reload_lore_mints
        await attach_reload_lore_mints(
            assistants, tail_seq=tail_seq,
            session_id=replay.get("lineage") or session_id,
        )
    return {"queue": status["queue"], "queue_seq": status["seq"]}
