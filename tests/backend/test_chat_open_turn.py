"""The reload's OPEN TURN.

The read-path fact the resync-parity drive stands on: the projection
replays a never-ended turn as a turn with no `end_seq`. The read path must
hand that turn's frames to the row that turn is writing and MARK it
(`open_turn`), so a reload mid-turn renders the open turn (the store re-seats
streaming) instead of a settled partial row. A row that carries
`halt` NEVER takes the mark: a dead turn's one record is the halt card (the
replay-split INVARIANT's read-side half — an open turn AND a halt card for
one row would be two records for one turn).

The crash halt contract (the plugin's read closes a crashed log with dsh's
interrupted-turn closers, live turns excluded): a dead
turn renders EXACTLY ONE halt record per reload whichever producer wins, and
a deadline breach keeps its timeout reason. The three scenarios live here
because they are this file's split seen from the dead side.
"""

import pytest


def row(mid, role="assistant", seq=None, created="t"):
    return {"message_id": mid, "role": role, "driver_seq": seq, "created_at": created}


def slim(mid, parent=None, role="user", created="t", seq=None):
    return {"mid": mid, "parent_id": parent, "role": role,
            "driver_seq": seq, "created_at": created}


def _dsh(seq, kind, data=None):
    return {"type": "dsh_event", "kind": kind, "seq": seq, "data": data}


class _FakeDB:
    """The anchor projection: u1 → a1(stamped 5) → u2 → a_open."""

    async def query(self, _q, _p=None, **_kw):
        return [
            slim("u1"),
            slim("a1", "u1", "assistant", "1", seq=5),
            slim("u2", "a1"),
            slim("a_open", "u2", "assistant", "2"),
        ]


def _replay(open_frames):
    return {
        "turns": [
            {"end_seq": 5, "frames": [_dsh(1, "assistant/message")]},
            # The trailing turn never ended — no end_seq (step 1's extension).
            {"frames": open_frames},
        ],
        "tail_seq": 9,
    }


async def _attach(out, replay, monkeypatch, db=None):
    import driver.timeline
    from routes.chat.messages import _attach_timeline

    async def fake_fetch(_session_id):
        return replay

    # monkeypatch (NOT a bare assign + del): the module-level function must
    # survive this test for every later test in the worker.
    monkeypatch.setattr(driver.timeline, "fetch_session_entries", fake_fetch)
    await _attach_timeline(db or _FakeDB(), {}, "chat-1", out, offset=0)


@pytest.mark.asyncio
async def test_open_turn_attaches_frames_and_marks_the_row(monkeypatch):
    """The trailing never-ended turn's frames land on the row that turn is
    writing, flagged `open_turn` — the reload's streaming re-seat input."""
    out = [row("a1", seq=5, created="1"), row("a_open", created="2")]
    await _attach(out, _replay([_dsh(7, "assistant/message")]), monkeypatch)
    assert out[1]["open_turn"] is True
    assert out[1]["frames"] == [_dsh(7, "assistant/message")]
    assert "open_turn" not in out[0]


@pytest.mark.asyncio
async def test_halted_row_never_takes_the_open_mark(monkeypatch):
    """A row with `halt` is a DEAD turn's record — the halt card owns it. The
    projection still shows the turn open (the log has no turn/end for it), so
    the split is the row's halt column: no frames from the open turn, no
    open_turn flag. Exactly one record renders."""
    out = [
        row("a1", seq=5, created="1"),
        {**row("a_open", created="2"), "halt": {"reason": "turn_timeout"}},
    ]
    await _attach(out, _replay([_dsh(7, "assistant/message")]), monkeypatch)
    assert "open_turn" not in out[1]
    # The halt card mint (tail-anchored) is the row's one record.
    assert out[1]["frames"][0]["type"] == "lore/halt"


@pytest.mark.asyncio
async def test_stamped_last_row_takes_no_open_turn(monkeypatch):
    """The last assistant row is STAMPED (its turn ended) while an open turn
    trails it (the driver runs a turn whose row bind is gone — a backend
    restart mid-turn). Attaching the open frames to the settled row would
    corrupt its window: the open turn renders only when a row claims it."""
    out = [
        row("a1", seq=5, created="1"),
        row("a2", seq=9, created="2"),
    ]
    await _attach(out, _replay([_dsh(7, "assistant/message")]), monkeypatch)
    assert "open_turn" not in out[1]
    assert out[1].get("frames") is None


@pytest.mark.asyncio
async def test_no_open_turn_no_mark(monkeypatch):
    """Every turn ended: no row is marked — the reload renders settled rows."""
    out = [row("a1", seq=5, created="1")]
    await _attach(out, {
        "turns": [{"end_seq": 5, "frames": [_dsh(1, "assistant/message")]}],
        "tail_seq": 5,
    }, monkeypatch)
    assert "open_turn" not in out[0]


@pytest.mark.asyncio
async def test_open_turn_carries_assistant_stream_verbatim(monkeypatch):
    """The open row keeps the streamed text.

    The plugin folds the open turn's live stream and serves the fold on the
    OPEN turn as `assistant_stream`; the read path passes it through VERBATIM
    beside `open_turn` (the browser re-seats the transient tail from it). A
    closed turn's row never carries one — the field belongs to the still-open
    attempt, and the pass-through reads the trailing open turn only."""
    baseline = {
        "revision": 3,
        "activeAttempt": {
            "attemptId": "a1", "startedAfterSeq": 6, "turn": 1, "step": 0,
            "nextIndex": 2,
            "stream": [
                {"type": "text-chunks", "time0": 7, "index": 0,
                 "dt": [1], "texts": ["Hel", "lo"]},
            ],
        },
    }
    out = [row("a1", seq=5, created="1"), row("a_open", created="2")]
    replay = _replay([_dsh(7, "assistant/message")])
    replay["turns"][1]["assistant_stream"] = baseline
    # Garbage on a CLOSED turn must never ride its row: the pass-through reads
    # the trailing open turn only.
    replay["turns"][0]["assistant_stream"] = {"revision": 999}
    await _attach(out, replay, monkeypatch)
    assert out[1]["assistant_stream"] == baseline
    assert "assistant_stream" not in out[0]


@pytest.mark.asyncio
async def test_open_row_without_baseline_carries_no_assistant_stream(monkeypatch):
    """A plugin reply without a fold (nothing streaming mid-turn) adds no key —
    the older wire shape stays valid."""
    out = [row("a1", seq=5, created="1"), row("a_open", created="2")]
    await _attach(out, _replay([_dsh(7, "assistant/message")]), monkeypatch)
    assert out[1]["open_turn"] is True
    assert "assistant_stream" not in out[1]


# ─── the crash halt contract: exactly ONE halt record per dead turn ──────────
# The plugin's read closes a crashed log with dsh's deterministic
# interrupted-turn closers (a turn the driver no longer has registered), so
# the replay carries the turn's OWN lore/halt mint at the synthetic turn/end
# seq + 0.7; the backend's producer (the row's halt column) mints only for
# rows the replay left frameless. Whichever side wins, a dead turn renders
# exactly one card — and a deadline breach keeps its timeout reason.


def _turn_end(seq, kind, turn=2):
    return _dsh(seq, "turn/end", {"turn": turn, "reason": {"kind": kind}})


def _halt_mint(seq, reason, turn=2):
    return {"type": "lore/halt", "seq": seq,
            "data": {"turn": turn, "reason": reason}, "ignorable": True}


def _crash_replay():
    """Turn 1 settled at 5; turn 2 closed by the closers at 9 with its mint."""
    return {"turns": [
        {"end_seq": 5, "frames": [_dsh(4, "assistant/message")]},
        {"end_seq": 9, "frames": [
            _dsh(8, "assistant/message"),
            _turn_end(9, "interrupted"),
            _halt_mint(9.7, "interrupted"),
        ]},
    ], "tail_seq": 9}


def _payload_halts(out):
    return [f for r in out for f in (r.get("frames") or [])
            if isinstance(f, dict) and f.get("type") == "lore/halt"]


@pytest.mark.asyncio
async def test_crash_never_resumed_renders_one_halt_card(monkeypatch):
    """(i) A crash never resumed: the channel's resync dispatched the
    synthetic turn/end, so the row is stamped (driver_seq = the closer's seq
    — the same deterministic seq dsh later persists) and carries the halt the
    turn/end arm wrote. The stamp resolves, the frames attach with the
    replay's mint — and the row's own halt column must not add a second
    card."""
    class _StampedDB:
        async def query(self, _q, _p=None, **_kw):
            return [slim("u1"), slim("a1", "u1", "assistant", "1", seq=9)]

    out = [{**row("a1", seq=9, created="1"),
            "halt": {"reason": "interrupted", "anchor_seq": 8, "turn": 2}}]
    await _attach(out, _crash_replay(), monkeypatch, db=_StampedDB())
    halts = _payload_halts(out)
    assert len(halts) == 1, "the dead turn's ONE record — not one per producer"
    assert halts[0]["seq"] == 9.7
    assert halts[0]["data"]["reason"] == "interrupted"
    assert "open_turn" not in out[0], "a closed turn never takes the open mark"


@pytest.mark.asyncio
async def test_crash_resumed_after_a_deadline_keeps_the_timeout_reason(monkeypatch):
    """(ii) The deadline fired first (halt turn_timeout, no stamp — the
    breach path never stamps), then dsh persisted the closers on resume and a
    later turn completed. The repaired turn's mint rides only through a
    stamp, and the dead row has none — its record stays the timeout card."""
    replay = _crash_replay()
    replay["turns"].append({"end_seq": 15, "frames": [
        _dsh(13, "assistant/message"),
        _turn_end(15, "completed", turn=3),
    ]})
    replay["tail_seq"] = 15

    class _TwoRowDB:
        async def query(self, _q, _p=None, **_kw):
            return [
                slim("u1"),
                slim("a1", "u1", "assistant", "1"),
                slim("u2", "a1"),
                slim("a2", "u2", "assistant", "2", seq=15),
            ]

    out = [
        {**row("a1", created="1"),
         "halt": {"reason": "turn_timeout", "anchor_seq": 8, "turn": 2, "steps": 3}},
        row("a2", seq=15, created="2"),
    ]
    await _attach(out, replay, monkeypatch, db=_TwoRowDB())
    halts = _payload_halts(out)
    assert len(halts) == 1, "one dead turn, one card"
    assert halts[0]["seq"] == 8.7
    assert halts[0]["data"]["reason"] == "turn_timeout", (
        "a deadline breach keeps the backend's timeout reason — the replay's "
        "'interrupted' mint never reaches an unstamped row"
    )
    assert out[1]["frames"][0] == _dsh(13, "assistant/message"), (
        "the later turn's frames still attach by stamp"
    )


@pytest.mark.asyncio
async def test_breach_with_a_dead_driver_one_halt_card_no_open_mark(monkeypatch):
    """(iii) A breach whose /stop never landed (the driver is dead): the log
    ends mid-turn, the read closes it with the closers, and the unstamped
    halt row keeps the backend's timeout card — the trailing turn being
    closed, no row takes the open mark, and the replay's rider-less mint is
    not a second card."""
    class _UnstampedDB:
        async def query(self, _q, _p=None, **_kw):
            return [slim("u1"), slim("a1", "u1", "assistant", "1")]

    out = [{**row("a1", created="1"),
            "halt": {"reason": "turn_timeout", "anchor_seq": 8, "turn": 2}}]
    await _attach(out, _crash_replay(), monkeypatch, db=_UnstampedDB())
    halts = _payload_halts(out)
    assert len(halts) == 1
    assert halts[0]["seq"] == 8.7, "anchored inside the dead turn, never at the tail"
    assert halts[0]["data"]["reason"] == "turn_timeout"
    assert "open_turn" not in out[0], "the closers closed the turn — nothing is open"


# ─── the driver-start window: a held turn lock marks the open turn ───────────
# Between create_completion taking the turn lock and dsh logging `turn/start`,
# the replay cannot show the turn; the held lock is what says it runs.
# INVARIANT: every test above runs with the lock FREE — the autouse per-test
# Redis FLUSHDB (conftest `_redis_isolation`) clears it, so they keep asserting
# exactly today's lock-free read. Why: the lock state is a second input to the
# open mark; a test that does not choose it must still measure one value.


async def _hold_lock(session_id="chat-1"):
    from turn_lock import acquire_turn_lock
    assert await acquire_turn_lock(session_id)


_EMPTY = {"turns": [], "tail_seq": None}


def _closed_replay():
    return {
        "turns": [{"end_seq": 5, "frames": [_dsh(1, "assistant/message")]}],
        "tail_seq": 5,
    }


@pytest.mark.asyncio
async def test_lock_held_first_turn_marks_the_new_row(monkeypatch):
    """A chat's FIRST turn, before dsh writes `turn/start`: the log is empty,
    the lock says the turn runs — its row is marked open with no frames."""
    await _hold_lock()
    out = [row("u1", role="user", created="1"), row("a_new", created="2")]
    await _attach(out, _EMPTY, monkeypatch)
    assert out[1]["open_turn"] is True
    assert out[1]["frames"] == []
    assert "assistant_stream" not in out[1]


@pytest.mark.asyncio
async def test_lock_held_later_turn_marks_the_unstamped_newest_row(monkeypatch):
    """A LATER turn before its `turn/start`: the trailing turn is the previous
    closed one. The new unstamped row takes the mark; the stamped row keeps
    its own frames and no mark."""
    await _hold_lock()
    out = [row("a1", seq=5, created="1"), row("a_open", created="2")]
    await _attach(out, _closed_replay(), monkeypatch)
    assert out[1]["open_turn"] is True
    assert out[1]["frames"] == []
    assert "open_turn" not in out[0]
    assert out[0]["frames"] == [_dsh(1, "assistant/message")]


@pytest.mark.asyncio
async def test_lock_held_before_rows_exist_marks_nothing(monkeypatch):
    """The lock is taken before the new rows are created: the newest assistant
    row is the previous turn's STAMPED row — it never takes the mark."""
    await _hold_lock()

    class _OneTurnDB:
        async def query(self, _q, _p=None, **_kw):
            return [slim("u1"), slim("a1", "u1", "assistant", "1", seq=5)]

    out = [row("a1", seq=5, created="1")]
    await _attach(out, _closed_replay(), monkeypatch, db=_OneTurnDB())
    assert "open_turn" not in out[0]


@pytest.mark.asyncio
async def test_lock_held_before_rows_exist_marks_an_abnormal_unstamped_row(monkeypatch):
    """Accepted risk, pinned: the lock is held before the new rows exist and
    the previous turn ended abnormally (unstamped, no halt). That old row is
    the newest and takes the mark until the new turn's `ids` frame re-seats
    the slot — cosmetic and sub-second."""
    await _hold_lock()
    out = [row("a_old", created="1")]
    await _attach(out, _EMPTY, monkeypatch)
    assert out[0]["open_turn"] is True


@pytest.mark.asyncio
async def test_lock_held_newest_row_halted_marks_nothing(monkeypatch):
    """The halt INVARIANT outranks the lock: a halted row is a dead turn's one
    record, and the older unstamped row is not the newest — nothing is marked."""
    await _hold_lock()
    out = [
        row("a1", seq=5, created="1"),
        row("a_older", created="2"),
        {**row("a_halt", created="3"), "halt": {"reason": "turn_timeout"}},
    ]

    class _HaltDB:
        async def query(self, _q, _p=None, **_kw):
            return [
                slim("u1"), slim("a1", "u1", "assistant", "1", seq=5),
                slim("u2", "a1"), slim("a_older", "u2", "assistant", "2"),
                slim("u3", "a_older"), slim("a_halt", "u3", "assistant", "3"),
            ]

    await _attach(out, _closed_replay(), monkeypatch, db=_HaltDB())
    assert not any(r.get("open_turn") for r in out)


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", [_EMPTY, _closed_replay()], ids=["empty", "closed"])
async def test_lock_free_marks_nothing(monkeypatch, replay):
    """Today's behaviour, pinned: no lock, no open turn in the log — no mark."""
    out = [row("a1", seq=5, created="1"), row("a_new", created="2")]
    await _attach(out, replay, monkeypatch)
    assert not any(r.get("open_turn") for r in out)


@pytest.mark.asyncio
async def test_lock_held_with_a_genuine_open_turn_keeps_the_timeline(monkeypatch):
    """Once dsh logged the turn, the timeline owns the mark: its frames and
    `assistant_stream` ride the row, and the lock branch does not re-mark."""
    await _hold_lock()
    out = [row("a1", seq=5, created="1"), row("a_open", created="2")]
    replay = _replay([_dsh(7, "assistant/message")])
    replay["turns"][1]["assistant_stream"] = {"revision": 1}
    await _attach(out, replay, monkeypatch)
    assert out[1]["open_turn"] is True
    assert out[1]["frames"] == [_dsh(7, "assistant/message")]
    assert out[1]["assistant_stream"] == {"revision": 1}
    assert "open_turn" not in out[0]
