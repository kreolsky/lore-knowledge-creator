"""The reload timeline attaches by the driver's OWN id, not by position.

# WHY: the positional zip (Nth assistant row ↔ Nth turn) predates step 5 of
plan collapse-the-editor-harness-layer and breaks on every forked thread.
Rows stamp their own turn's boundary (messages.driver_seq), and the
projection returns each turn's end_seq — the read path keys rows to turns BY
STAMP. Since plan chat-branch-sessions a session's rows resolve only against
the session's OWN log (dsh id = Lore id; a seeded branch's log carries the
copied prefix under the SAME seqs), the per-session stamp map is the whole
pairing — no lineage walk (the log can hold no other branch's turns, and a
seq never restarts inside one log). A branch whose own log does not exist yet
reads its seed source's log cut at the seed boundary instead. An abnormally
ended turn (no stamp) pairs by its halt's anchor seq; rows that resolve
neither way (a pre-harness turn) keep no `frames` key.

Step 3 (plan lore-renders-dsh-conversation) adds the reload LORE MINTS: the
backend-product facts the verbatim replay cannot state — the halt column's
card at the log tail, the gen_steps image runs at their dispatching calls,
the compaction mint outcomes — ride the rows' `frames` beside the replayed
ones, so the browser's ONE replaceWindow input carries both.
"""


import pytest
from routes.chat.timeline_attach import _assign_frames_by_stamp


def row(mid, role="assistant", seq=None, created="t"):
    return {"message_id": mid, "role": role, "driver_seq": seq, "created_at": created}


def _dsh(seq, kind, data=None):
    return {"type": "dsh_event", "kind": kind, "seq": seq, "data": data}


@pytest.mark.asyncio
async def test_linear_thread_each_row_gets_its_own_turn():
    a1, a2 = row("a1", seq=5, created="1"), row("a2", seq=12, created="2")
    turns = [
        {"end_seq": 5, "frames": [_dsh(1, "assistant/message")]},
        {"end_seq": 12, "frames": [_dsh(2, "assistant/message")]},
    ]
    _assign_frames_by_stamp([a1, a2], turns)
    assert a1["frames"] == [_dsh(1, "assistant/message")]
    assert a2["frames"] == [_dsh(2, "assistant/message")]


@pytest.mark.asyncio
async def test_a_row_whose_turn_is_not_in_this_log_keeps_no_frames():
    """A stamp the read log does not hold resolves against nothing — the row
    stays text-only rather than borrowing another turn's frames."""
    a1 = row("a1", seq=83, created="1")        # copied prefix row
    turns = []                                  # the branch's log: no turns
    _assign_frames_by_stamp([a1], turns)
    assert a1.get("frames") is None


@pytest.mark.asyncio
async def test_unstamped_row_mid_chain_gets_nothing_and_breaks_nothing():
    """An abnormally ended turn (no turn/end → no stamp) stays frameless; the
    stamped rows around it still match their own turns."""
    a1, ab, a3 = row("a1", seq=5, created="1"), row("ab", created="2"), row("a3", seq=12, created="3")
    turns = [
        {"end_seq": 5, "frames": [_dsh(1, "assistant/message")]},
        {"end_seq": 12, "frames": [_dsh(2, "assistant/message")]},
    ]
    _assign_frames_by_stamp([a1, ab, a3], turns)
    assert a1["frames"] == [_dsh(1, "assistant/message")]
    assert ab.get("frames") is None
    assert a3["frames"] == [_dsh(2, "assistant/message")]


# TEST-WHY: recurring-regression — a turn that died without turn/end lost its whole timeline on reload (prod 2026-10-31, deadline breach).
@pytest.mark.asyncio
async def test_halted_row_takes_its_dead_turn_by_anchor_and_never_a_stamped_one():
    """A halted unstamped row pairs with the turn holding its anchor seq —
    closed by dsh's closers (an unstamped end_seq) or still open — and a halt
    whose anchor sits in a STAMPED turn takes nothing."""
    a1 = row("a1", seq=5, created="1")
    dead = {**row("dead", created="2"), "halt": {"reason": "turn_timeout", "anchor_seq": 9}}
    stray = {**row("stray", created="3"), "halt": {"reason": "error", "anchor_seq": 2}}
    turns = [
        {"end_seq": 5, "frames": [_dsh(2, "assistant/message")]},
        {"end_seq": 10, "frames": [_dsh(7, "tool/call"), _dsh(9, "tool/result")]},
    ]
    _assign_frames_by_stamp([a1, dead, stray], turns)
    assert a1["frames"] == [_dsh(2, "assistant/message")]
    assert dead["frames"] == [_dsh(7, "tool/call"), _dsh(9, "tool/result")]
    assert stray.get("frames") is None

    open_dead = {**row("od"), "halt": {"reason": "turn_timeout", "anchor_seq": 21}}
    _assign_frames_by_stamp([open_dead], [{"frames": [_dsh(20, "tool/call"), _dsh(21, "tool/result")]}])
    assert open_dead["frames"] == [_dsh(20, "tool/call"), _dsh(21, "tool/result")]


# TEST-WHY: recurring-regression — the dead turn's card showed the /stop closer's "aborted" instead of the stored turn_timeout.
@pytest.mark.asyncio
async def test_dead_turn_card_carries_the_rows_reason_not_the_stop_closers():
    from driver.frames import attach_reload_lore_mints
    dead = {**row("dead"), "halt": {"reason": "turn_timeout", "anchor_seq": 9, "turn": 1}}
    closer_halt = {"type": "lore/halt", "seq": 10.7, "data": {"reason": "aborted"}}
    turns = [{"end_seq": 10, "frames": [_dsh(9, "tool/result"), _dsh(10, "turn/end"), closer_halt]}]
    _assign_frames_by_stamp([dead], turns)
    await attach_reload_lore_mints([dead], tail_seq=10, session_id="s")
    halts = [(f["seq"], f["data"]["reason"]) for f in dead["frames"] if f.get("type") == "lore/halt"]
    assert halts == [(9.7, "turn_timeout")]
    assert [f["seq"] for f in dead["frames"]] == [9, 9.7, 10]


@pytest.mark.asyncio
async def test_no_stamps_no_turns_no_attachment():
    """A pre-harness thread (no stamps, no log) keeps its rows untouched —
    the client renders what the row carries."""
    a1 = row("a1")
    _assign_frames_by_stamp([a1], [{"end_seq": 5, "frames": [_dsh(1, "x")]}])
    assert a1.get("frames") is None
    _assign_frames_by_stamp([row("a1", seq=5)], [])
    assert a1.get("frames") is None


@pytest.mark.asyncio
async def test_the_replayed_transport_terminal_never_rides_the_rows():
    """The replay of a NON-live session ends every closed turn with a
    seq-anchored `turn_closed` (plugin entries.ts `terminals`) — TRANSPORT
    state for the resync consumer, not timeline content: the rows' frames are
    the assembler's input, so the stamp pass drops it here. Riding it would
    feed the browser's frame reducer a terminal inside a reload."""
    a1 = row("a1", seq=5, created="1")
    turns = [{"end_seq": 5, "frames": [
        _dsh(1, "assistant/message"),
        _dsh(5, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        {"type": "turn_closed", "seq": 5.9},
    ]}]
    _assign_frames_by_stamp([a1], turns)
    assert a1["frames"] == [
        _dsh(1, "assistant/message"),
        _dsh(5, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
    ]


@pytest.mark.asyncio
async def test_attach_unseeded_branch_reads_the_seed_source_cut_at_the_boundary(
    monkeypatch,
):
    """A branch whose own log does not exist yet (seed stamp still set — a
    rewind not followed by a send, or a first turn that failed) replays its
    copied prefix from the SEED SOURCE log: the copies keep the source's seqs,
    so the prefix rows get their frames (chips) back. Only closed turns at or
    before the boundary count — the source's later turns and its live open
    turn are never grafted onto the branch."""
    import driver.timeline
    from routes.chat.timeline_attach import _attach_timeline

    asked: list[str] = []

    async def fake_fetch(session_id):
        asked.append(session_id)
        return {"turns": [
            {"end_seq": 5, "frames": [_dsh(1, "tool/call")]},
            {"end_seq": 12, "frames": [_dsh(8, "tool/call")]},   # past the cut
            {"end_seq": None, "frames": [_dsh(14, "assistant/message")]},  # source's open turn
        ], "tail_seq": 14}

    monkeypatch.setattr(driver.timeline, "fetch_session_entries", fake_fetch)
    prefix = row("a1", seq=5, created="1")
    pending = row("a2", created="2")   # the branch's own unstamped row
    await _attach_timeline(
        {"seed_source_session": "source-log", "seed_source_seq": 5},
        "branch-chat", [prefix, pending], offset=0,
    )
    assert asked == ["source-log"]
    assert prefix["frames"] == [_dsh(1, "tool/call")]
    assert pending.get("frames") is None
    assert pending.get("open_turn") is None
