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


def _dsh(seq, kind, data=None):
    return {"type": "dsh_event", "kind": kind, "seq": seq, "data": data}


def _replay(open_frames):
    return {
        "turns": [
            {"end_seq": 5, "frames": [_dsh(1, "assistant/message")]},
            # The trailing turn never ended — no end_seq (step 1's extension).
            {"frames": open_frames},
        ],
        "tail_seq": 9,
    }


async def _attach(out, replay, monkeypatch, db=None, running=False):
    import driver.timeline
    from routes.chat.timeline_attach import _attach_timeline

    async def fake_fetch(_session_id):
        return replay

    async def fake_status(_session_id):
        return {"status": "running" if running else "idle", "queue": [], "seq": None}

    # monkeypatch (NOT a bare assign + del): the module-level function must
    # survive this test for every later test in the worker.
    monkeypatch.setattr(driver.timeline, "fetch_session_entries", fake_fetch)
    monkeypatch.setattr(driver.timeline, "fetch_session_status", fake_status)
    await _attach_timeline({}, "chat-1", out, offset=0)


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


# ─── the claim-to-log window: a running dsh agent marks the open turn ────────
# Between dsh's claim and its log flush the replay cannot show the turn; the
# agent's running status is what says it runs. Every test above reads an IDLE
# status (`_attach`'s default), so they keep asserting the idle read.


@pytest.mark.asyncio
async def test_running_with_a_genuine_open_turn_keeps_the_timeline(monkeypatch):
    """Once dsh logged the turn, the timeline owns the mark: its frames and
    `assistant_stream` ride the row, and the status branch does not re-mark."""
    out = [row("a1", seq=5, created="1"), row("a_open", created="2")]
    replay = _replay([_dsh(7, "assistant/message")])
    replay["turns"][1]["assistant_stream"] = {"revision": 1}
    await _attach(out, replay, monkeypatch, running=True)
    assert out[1]["open_turn"] is True
    assert out[1]["frames"] == [_dsh(7, "assistant/message")]
    assert out[1]["assistant_stream"] == {"revision": 1}
    assert "open_turn" not in out[0]
