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
reads its seed source's log cut at the seed boundary instead. Rows whose
stamp does not resolve (a pre-harness turn, an abnormally ended turn) keep no
`frames` key.

Step 3 (plan lore-renders-dsh-conversation) adds the reload LORE MINTS: the
backend-product facts the verbatim replay cannot state — the halt column's
card at the log tail, the gen_steps image runs at their dispatching calls,
the compaction mint outcomes — ride the rows' `frames` beside the replayed
ones, so the browser's ONE replaceWindow input carries both.
"""

import json

import pytest
from routes.chat.messages import _assign_frames_by_stamp


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
async def test_attach_reads_the_driver_session_the_turns_ran_under(monkeypatch):
    """A compaction continuation chat runs its turns under the SOURCE session's
    log (`completions.py`: `compacted_from or session_id`). The timeline read
    must name the SAME id — the driver has never seen the continuation's own
    chat id, so asking for it yields an empty turn list and the rows never get
    frames (observed class: live stream shows chips, reload loses them)."""
    import driver.timeline
    from routes.chat.messages import _attach_timeline

    asked: list[str] = []

    async def fake_fetch(session_id):
        asked.append(session_id)
        return {"turns": [{"end_seq": 5, "frames": [_dsh(1, "assistant/message")]}],
                "tail_seq": 5}

    monkeypatch.setattr(driver.timeline, "fetch_session_entries", fake_fetch)
    out = [row("a1", seq=5)]
    # _attach_timeline imports fetch_session_entries lazily from the module
    await _attach_timeline(
        {"compacted_from": "source-session"}, "continuation-chat", out,
        offset=0,
    )
    assert asked == ["source-session"], (
        "the read must use the lineage the turns were written under"
    )
    assert out[0]["frames"] == [_dsh(1, "assistant/message")]


@pytest.mark.asyncio
async def test_attach_plain_chat_reads_its_own_id(monkeypatch):
    """A normal chat (no compacted_from) keeps reading its own id — unchanged."""
    import driver.timeline
    from routes.chat.messages import _attach_timeline

    asked: list[str] = []

    async def fake_fetch(session_id):
        asked.append(session_id)
        return {"turns": [{"end_seq": 5, "frames": [_dsh(1, "assistant/message")]}],
                "tail_seq": 5}

    monkeypatch.setattr(driver.timeline, "fetch_session_entries", fake_fetch)
    out = [row("a1", seq=5)]
    await _attach_timeline({}, "plain-chat", out, offset=0)
    assert asked == ["plain-chat"]
    assert out[0]["frames"] == [_dsh(1, "assistant/message")]


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
    from routes.chat.messages import _attach_timeline

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


@pytest.mark.asyncio
async def test_attach_half_stamped_branch_reads_its_own_id(monkeypatch):
    """The migration's 422 marker (seed source, no seq) names no boundary to
    cut at — the read stays on the branch's own id, never an uncut source."""
    import driver.timeline
    from routes.chat.messages import _attach_timeline

    asked: list[str] = []

    async def fake_fetch(session_id):
        asked.append(session_id)
        return {"turns": [], "tail_seq": None}

    monkeypatch.setattr(driver.timeline, "fetch_session_entries", fake_fetch)
    await _attach_timeline(
        {"seed_source_session": "source-log"}, "branch-chat",
        [row("a1", seq=5)], offset=0,
    )
    assert asked == ["branch-chat"]


# ─── the reload lore mints, through the attach ────────────────────────────────


@pytest.mark.asyncio
async def test_attach_mints_the_halt_card_for_the_turnless_row(monkeypatch):
    """A turn that ended without a turn/end (deadline breach, disconnect) has
    no replayed turn — its record is the row's `halt` column, minted at the
    log tail (tail_seq) so the browser renders the card where the turn was."""
    import driver.timeline
    from routes.chat.messages import _attach_timeline

    async def fake_fetch(session_id):
        # The trailing turn never ended: no entry, but the log has a tail.
        return {"turns": [], "tail_seq": 30}

    monkeypatch.setattr(driver.timeline, "fetch_session_entries", fake_fetch)
    out = [{**row("a1", created="1"), "halt": {"reason": "turn_timeout", "steps": 3}}]
    await _attach_timeline({}, "chat-1", out, offset=0)
    mint = out[0]["frames"][0]
    assert mint["type"] == "lore/halt"
    assert mint["seq"] == 30.7
    assert mint["data"] == {"turn": None, "reason": "turn_timeout", "steps": 3}


@pytest.mark.asyncio
async def test_attach_mints_image_gen_from_gen_steps(monkeypatch):
    """The detached generation's card: anchored at the run's dispatching call
    inside the row's own replayed turn — the same anchor the live WS mint
    uses, derived from the SAME gen_steps the WS event carries."""
    import driver.timeline
    from routes.chat.messages import _attach_timeline
    from test_driver_frames import _stub_live_refs

    # The deleted-image filter's DB seam — all live (the filter has its own
    # tests in test_driver_frames.py).
    _stub_live_refs(monkeypatch)
    call = _dsh(3, "tool/call", {"turn": 1, "step": 1, "callId": "cg",
                                 "name": "generate_image", "arguments": "{}"})
    result = _dsh(4, "tool/result", {"message": {
        "role": "tool", "source": {"kind": "tool", "callId": "cg"},
        "toolCallId": "cg", "content": [{"type": "text",
                                         "text": json.dumps({"status": "generating", "run_id": "r1"})}]}})

    async def fake_fetch(session_id):
        return {"turns": [{"end_seq": 9, "frames": [call, result,
                _dsh(9, "turn/end", {"turn": 1, "reason": {"kind": "completed"}})]}],
                "tail_seq": 9}

    monkeypatch.setattr(driver.timeline, "fetch_session_entries", fake_fetch)
    out = [{**row("a1", seq=9), "gen_steps": [
        {"tool": "generate_image", "run_id": "r1", "call_id": "cg",
         "image_ref_ids": ["ref-1"], "title": "Мир"}]}]
    await _attach_timeline({}, "chat-1", out, offset=0)
    frames = out[0]["frames"]
    mint = next(f for f in frames if f.get("type") == "lore/image-gen")
    assert mint["seq"] == 3.6
    assert frames.index(mint) == frames.index(call) + 1
    assert mint["data"]["imageRefIds"] == ["ref-1"]
    assert mint["data"]["title"] == "Мир"


@pytest.mark.asyncio
async def test_attach_mints_nothing_for_a_pre_harness_thread(monkeypatch):
    """No turns AND no tail: the driver never logged anything for this
    lineage — the attach must not even mint a halt (no honest anchor)."""
    import driver.timeline
    from routes.chat.messages import _attach_timeline

    async def fake_fetch(session_id):
        return {"turns": [], "tail_seq": None}

    monkeypatch.setattr(driver.timeline, "fetch_session_entries", fake_fetch)
    out = [{**row("a1"), "halt": {"reason": "disconnected"}}]
    await _attach_timeline({}, "chat-1", out, offset=0)
    assert out[0].get("frames") is None
