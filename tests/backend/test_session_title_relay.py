"""The harness titler's relay: a `session/title` dsh event → chat_sessions.title.

The title is minted dsh-side (session-title-first-prompt-llm, deterministic
fallback first) and arrives as a verbatim `dsh_event` frame, kind
`session/title` — the plugin relays it like any other event (plan
lore-renders-dsh-conversation step 3). The LEAK GUARD moved backend-side with
the translation's death: the tokenizer-leak patterns the retired map.ts guard
carried live here now, where the write is. The write lands in
driver.persistence._persist_session_title, called from driver.frames' arm.
Guards under test:

- the USER pin (`title_user_set`, set by the PATCH rename path) blocks the
  automatic revision — a single guarded UPDATE, so a PATCH racing the relay
  cannot interleave set-then-overwrite;
- an event carrying no usable title never reaches the DB;
- a tokenizer-leak title (unused vocab slots, raw byte tokens) never reaches
  the user — no write, no frame, the row keeps the deterministic fallback;
- a persist failure never breaks the turn;
- the arm emits NO verbatim dsh_event: the naming decision shows no chip —
  the typed `session_title` frame is the title's transport ONLY, and only
  when the write landed. The arm's frames are DICTS (the WS chat_frame
  vocabulary) since the SSE wire serialization died with the pump (plan
  agent-line-harness-lifecycle step 9).
"""
from __future__ import annotations

import driver.frames  # seam home since the driver.client mechanical split: _persist_session_title
import driver.persistence


class _AsyncBox:
    """Awaitable wrapper so `await get_db()` yields the fake db (get_db is async)."""

    def __init__(self, db) -> None:
        self._db = db

    def __await__(self):
        async def _():
            return self._db
        return _().__await__()


class _FakeDB:
    def __init__(self, rows) -> None:
        self._rows = rows
        self.executed: list[tuple] = []

    async def query(self, sql, params):
        self.executed.append((sql, params))
        return self._rows


async def _noop(*_a, **_k):
    return None


def _turn(session_id: str = "chat-1") -> "driver.frames._TurnProjection":
    return driver.frames._TurnProjection(
        assistant_msg_id="m1", persist_content=_noop,
        persist_sources=_noop, persist_extras=_noop, session_id=session_id,
    )


def _title_event(title) -> dict:
    """The verbatim dsh event the plugin relays for the harness titler."""
    return {"type": "dsh_event", "kind": "session/title", "seq": 8,
            "data": {"title": title, "source": "model"}}


# ─── driver.persistence._persist_session_title ─────────────────────────────────


async def test_persist_session_title_runs_guarded_update(monkeypatch):
    """The write is ONE guarded UPDATE on chat_sessions: title + updated_at,
    WHERE the pin flag is falsy — a user rename cannot be overwritten."""
    executed: list[tuple] = []
    db = _FakeDB(rows=[{"id": "chat-1"}])

    async def fake_get_db():
        executed.append(("db", None))
        return db

    monkeypatch.setattr(driver.persistence, "get_db", fake_get_db)
    written = await driver.persistence._persist_session_title("chat-1", "Fresh Title")
    assert written is True
    sql, params = db.executed[0]
    assert "UPDATE" in sql and "chat_sessions" in sql
    assert "title" in sql and "updated_at" in sql
    assert "title_user_set" in sql
    assert params == {"sid": "chat-1", "t": "Fresh Title"}


async def test_persist_session_title_skips_empty_title(monkeypatch):
    """An empty/whitespace title writes nothing (keeps the previous row value)."""
    db = _FakeDB(rows=[{"id": "chat-1"}])
    monkeypatch.setattr(driver.persistence, "get_db", lambda: _AsyncBox(db))
    assert await driver.persistence._persist_session_title("chat-1", "   ") is False
    assert await driver.persistence._persist_session_title("chat-1", "") is False
    assert db.executed == []


async def test_persist_session_title_skips_missing_session(monkeypatch):
    """No session id → no write (the relay arm logs instead)."""
    db = _FakeDB(rows=[])
    monkeypatch.setattr(driver.persistence, "get_db", lambda: _AsyncBox(db))
    assert await driver.persistence._persist_session_title("", "Title") is False
    assert db.executed == []


async def test_persist_session_title_reports_pinned_row_as_not_written(monkeypatch):
    """A pinned row (title_user_set) matches zero rows → the guard held the
    user's title; the relay reports not-written instead of failing."""
    db = _FakeDB(rows=[])
    monkeypatch.setattr(driver.persistence, "get_db", lambda: _AsyncBox(db))
    assert await driver.persistence._persist_session_title("chat-1", "Revision") is False


# ─── driver.frames session_title arm ─────────────────────────────────────────


async def test_a_written_title_is_relayed_to_the_open_client(monkeypatch):
    """The row is written AND the frame reaches the browser: the open client
    holds the sessions list in a store the DB write cannot reach, so the relay
    is what renames the row without a second fetch."""
    calls: list[tuple] = []

    async def fake_persist(session_id, title):
        calls.append((session_id, title))
        return True

    monkeypatch.setattr(driver.persistence, "_persist_session_title", fake_persist)
    turn = _turn("chat-9")
    frames = await driver.frames._relay_frame(turn, _title_event("Lore Chat"))
    assert calls == [("chat-9", "Lore Chat")]
    assert len(frames) == 1
    # Frame DICT (the WS vocabulary): the fan-out envelops this dict as a
    # chat_frame on the project WS — no SSE wire serialization exists anymore.
    assert frames[0] == {"type": "session_title", "title": "Lore Chat"}
    # The verbatim dsh_event does NOT ride (a naming decision shows no chip).
    assert turn.steps_count == 0


async def test_an_event_with_no_usable_title_never_reaches_the_db(monkeypatch):
    """A missing or non-string title degrades to no write, and the empty-title
    guard inside _persist_session_title covers the blank string."""
    queries: list[tuple] = []

    class _CountingDB:
        async def query(self, sql, params):
            queries.append((sql, params))
            return [{"id": "chat-1"}]

    monkeypatch.setattr(driver.persistence, "get_db", lambda: _AsyncBox(_CountingDB()))
    for bad in (None, 42, {"title": "nested"}, "", "   "):
        assert await driver.frames._relay_frame(_turn(), _title_event(bad)) == []
    assert queries == [], "an event with no usable title must not reach the DB"


async def test_a_tokenizer_leak_title_never_reaches_the_user(monkeypatch):
    """The leak guard moved here with the translation's death: Gemma
    SentencePiece unused slots and raw byte tokens must never land in the chat
    list — no write, no frame, the row keeps the deterministic fallback."""
    queries: list[tuple] = []

    class _CountingDB:
        async def query(self, sql, params):
            queries.append((sql, params))
            return [{"id": "chat-1"}]

    monkeypatch.setattr(driver.persistence, "get_db", lambda: _AsyncBox(_CountingDB()))
    for leak in ("<unused49><unused49> the document", "<0x1F600> title"):
        assert await driver.frames._relay_frame(_turn(), _title_event(leak)) == []
    assert queries == [], "a leaking title must not reach the DB"
    # The guard is on the TRIMMED title: whitespace around a leak is still one.
    assert await driver.frames._relay_frame(
        _turn(), _title_event("  <unused49>  ")) == []


async def test_a_real_title_passes_the_leak_guard(monkeypatch):
    """The patterns are anchored to the token shape: prose containing the word
    'unused' or hex-adjacent text is a legitimate title."""
    async def fake_persist(session_id, title):
        return True

    monkeypatch.setattr(driver.persistence, "_persist_session_title", fake_persist)
    turn = _turn()
    frames = await driver.frames._relay_frame(turn, _title_event("Unused corridors of the keep"))
    assert frames == [{"type": "session_title", "title": "Unused corridors of the keep"}]


async def test_persist_failure_does_not_break_the_turn(monkeypatch):
    """Best-effort: the persist raising leaves the turn intact — the row keeps
    the deterministic fallback title and the turn continues."""
    async def boom(session_id, title):
        raise RuntimeError("db down")

    monkeypatch.setattr(driver.persistence, "_persist_session_title", boom)
    assert await driver.frames._relay_frame(_turn(), _title_event("T")) == []
    # Nothing is relayed either: the list must never show a title the row
    # does not hold.


async def test_a_pinned_row_is_reported_not_written(monkeypatch):
    """The pin guard lives in the UPDATE; a refused revision relays NOTHING —
    a user-renamed chat must not flicker to the model's name and back on the
    next reload."""
    async def pinned(session_id, title):
        return False

    monkeypatch.setattr(driver.persistence, "_persist_session_title", pinned)
    assert await driver.frames._relay_frame(_turn(), _title_event("Revision")) == []
