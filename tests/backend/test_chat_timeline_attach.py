"""The reload timeline attaches by the driver's OWN id, not by position.

# WHY: the positional zip (Nth assistant row ↔ Nth turn) predates step 5 of
plan collapse-the-editor-harness-layer and breaks on every forked thread: the
dsh log holds only the CURRENT lineage, so a mid-thread fork lands the fork
turn's frames on the abandoned branch's row and leaves the fork row itself
frameless — observed live on gray (row seq=98 carrying 11 foreign frames; the
fork row seq=107 with none). Rows already stamp their own turn's boundary
(messages.driver_seq, the same seq /session-leaf takes), and the projection
now returns each turn's end_seq — so the read path keys rows to turns BY
STAMP, anchored to the active line (a root fork renumbers seqs, so a bare
seq dict would mis-assign old-branch rows whose stamps collide).

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


def slim(mid, parent=None, role="user", created="t", seq=None):
    """The anchor projection _attach_timeline fetches (mid/parent/role/
    driver_seq/created_at — the walk needs the stamp on the FULL set, not
    just the page)."""
    return {"mid": mid, "parent_id": parent, "role": role,
            "driver_seq": seq, "created_at": created}


def _dsh(seq, kind, data=None):
    return {"type": "dsh_event", "kind": kind, "seq": seq, "data": data}


@pytest.mark.asyncio
async def test_linear_thread_each_row_gets_its_own_turn():
    a1, a2 = row("a1", seq=5, created="1"), row("a2", seq=12, created="2")
    turns = [
        {"end_seq": 5, "frames": [_dsh(1, "assistant/message")]},
        {"end_seq": 12, "frames": [_dsh(2, "assistant/message")]},
    ]
    all_rows = [slim("u1"), slim("a1", "u1", "assistant", "1", seq=5),
                slim("u2", "a1"), slim("a2", "u2", "assistant", "2", seq=12)]
    _assign_frames_by_stamp([a1, a2], turns, all_rows)
    assert a1["frames"] == [_dsh(1, "assistant/message")]
    assert a2["frames"] == [_dsh(2, "assistant/message")]


@pytest.mark.asyncio
async def test_fork_off_branch_row_keeps_no_foreign_frames():
    """The observed gap: after a fork at A1 the abandoned row (seq=98) must
    NOT receive the fork turn's frames, and the fork row must get its own."""
    a1 = row("a1", seq=83, created="1")
    a2 = row("a2", seq=98, created="2")       # abandoned branch
    a2p = row("a2p", seq=107, created="3")    # the fork turn's row
    turns = [
        {"end_seq": 83, "frames": [_dsh(1, "assistant/message")]},
        # seq 98's turn is GONE from the re-seeded lineage
        {"end_seq": 107, "frames": [_dsh(2, "assistant/message")]},
    ]
    all_rows = [
        slim("u1"), slim("a1", "u1", "assistant", "1", seq=83),
        slim("u2", "a1"), slim("a2", "u2", "assistant", "2", seq=98),
        slim("u3", "a1"), slim("a2p", "u3", "assistant", "3", seq=107),
    ]
    _assign_frames_by_stamp([a1, a2, a2p], turns, all_rows)
    assert a1["frames"] == [_dsh(1, "assistant/message")]
    assert a2.get("frames") is None       # honest: its turn no longer exists
    assert a2p["frames"] == [_dsh(2, "assistant/message")]


@pytest.mark.asyncio
async def test_root_fork_seq_collision_resolved_by_the_active_line():
    """A root fork RESETS to an empty log whose seqs restart low, so an
    abandoned row's OLD stamp can equal an active turn's end_seq (here: both
    5). Only the chain anchored at the NEWEST stamped row (the current
    lineage's head) may receive frames."""
    a1 = row("a1", seq=5, created="1")          # old prefix row, stamp collides
    a_old = row("a_old", seq=12, created="2")   # old tail row, stamp unmatched
    a_new = row("a_new", seq=5, created="4")    # the fresh lineage's head
    turns = [{"end_seq": 5, "frames": [_dsh(1, "assistant/message")]}]
    # old branch: u1→a1→u2→a_old; root fork: u3→a_new (parent null)
    all_rows = [
        slim("u1"), slim("a1", "u1", "assistant", "1", seq=5),
        slim("u2", "a1"), slim("a_old", "u2", "assistant", "2", seq=12),
        slim("u3"), slim("a_new", "u3", "assistant", "4", seq=5),
    ]
    _assign_frames_by_stamp([a1, a_old, a_new], turns, all_rows)
    assert a1.get("frames") is None    # stamp matches, row is NOT on the line
    assert a_old.get("frames") is None
    assert a_new["frames"] == [_dsh(1, "assistant/message")]


@pytest.mark.asyncio
async def test_unstamped_row_mid_chain_gets_nothing_and_breaks_nothing():
    """An abnormally ended turn (no turn/end → no stamp) stays frameless; the
    stamped rows around it still match their own turns."""
    a1, ab, a3 = row("a1", seq=5, created="1"), row("ab", created="2"), row("a3", seq=12, created="3")
    turns = [
        {"end_seq": 5, "frames": [_dsh(1, "assistant/message")]},
        {"end_seq": 12, "frames": [_dsh(2, "assistant/message")]},
    ]
    all_rows = [
        slim("u1"), slim("a1", "u1", "assistant", "1", seq=5),
        slim("u2", "a1"), slim("ab", "u2", "assistant", "2"),
        slim("u3", "ab"), slim("a3", "u3", "assistant", "3", seq=12),
    ]
    _assign_frames_by_stamp([a1, ab, a3], turns, all_rows)
    assert a1["frames"] == [_dsh(1, "assistant/message")]
    assert ab.get("frames") is None
    assert a3["frames"] == [_dsh(2, "assistant/message")]


@pytest.mark.asyncio
async def test_no_stamps_no_turns_no_attachment():
    """A pre-harness thread (no stamps, no log) keeps its rows untouched —
    the client renders what the row carries."""
    a1 = row("a1")
    _assign_frames_by_stamp([a1], [{"end_seq": 5, "frames": [_dsh(1, "x")]}],
                            [slim("u1"), slim("a1", "u1", "assistant", "1", seq=5)])
    assert a1.get("frames") is None
    _assign_frames_by_stamp([row("a1", seq=5)], [], [])
    assert a1.get("frames") is None


class _FakeDB:
    async def query(self, _q, _p=None, **_kw):
        # The anchor projection: u1 → a1(stamped 5)
        return [slim("u1"), slim("a1", "u1", "assistant", "1", seq=5)]


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
        _FakeDB(), {"compacted_from": "source-session"}, "continuation-chat", out,
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
    await _attach_timeline(_FakeDB(), {}, "plain-chat", out, offset=0)
    assert asked == ["plain-chat"]
    assert out[0]["frames"] == [_dsh(1, "assistant/message")]


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

    class _OneRowDB:
        async def query(self, _q, _p=None, **_kw):
            return [slim("u1"), slim("a1", "u1", "assistant", "1")]

    out = [{**row("a1", created="1"), "halt": {"reason": "turn_timeout", "steps": 3}}]
    await _attach_timeline(_OneRowDB(), {}, "chat-1", out, offset=0)
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

    class _OneRowDB:
        async def query(self, _q, _p=None, **_kw):
            return [slim("u1"), slim("a1", "u1", "assistant", "1", seq=9)]

    out = [{**row("a1", seq=9), "gen_steps": [
        {"tool": "generate_image", "run_id": "r1", "call_id": "cg",
         "image_ref_ids": ["ref-1"], "title": "Мир"}]}]
    await _attach_timeline(_OneRowDB(), {}, "chat-1", out, offset=0)
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

    class _OneRowDB:
        async def query(self, _q, _p=None, **_kw):
            return [slim("u1"), slim("a1", "u1", "assistant", "1")]

    out = [{**row("a1"), "halt": {"reason": "disconnected"}}]
    await _attach_timeline(_OneRowDB(), {}, "chat-1", out, offset=0)
    assert out[0].get("frames") is None
