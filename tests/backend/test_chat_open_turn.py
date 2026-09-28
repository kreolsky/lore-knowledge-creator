"""Plan agent-line-harness-lifecycle step 8 — the reload's OPEN TURN (the
flip/migration half retired with the lifecycle flag in step 9).

The read-path fact the resync-parity drive stands on: the projection (step 1)
replays a never-ended turn as a turn with no `end_seq`. The read path must
hand that turn's frames to the row that turn is writing and MARK it
(`open_turn`), so a reload mid-turn renders the open turn (the store re-seats
streaming — Decision 16) instead of a settled partial row. A row that carries
`halt` NEVER takes the mark: a dead turn's one record is the halt card (the
replay-split INVARIANT's read-side half — an open turn AND a halt card for
one row would be two records for one turn).
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


async def _attach(out, replay, monkeypatch):
    import driver.timeline
    from routes.chat.messages import _attach_timeline

    async def fake_fetch(_session_id):
        return replay

    # monkeypatch (NOT a bare assign + del): the module-level function must
    # survive this test for every later test in the worker.
    monkeypatch.setattr(driver.timeline, "fetch_session_entries", fake_fetch)
    await _attach_timeline(_FakeDB(), {}, "chat-1", out, offset=0)


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
