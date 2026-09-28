"""list_sessions ordering by last-message time + last_message_at on AI chats.

# ARCH (plan: chat-sort-by-last-message): chat sessions are now sorted by the
# time of their LAST message (newest first), derived read-time from the
# messages table — not by the stale chat_sessions.updated_at (which is never
# bumped on the AI completion path). last_message_at is serialized for EVERY
# session (notes + AI), not just notes. These tests establish the list_sessions
# mock-DB test pattern: patch get_document_access / get_db / fetch_one at the
# sessions module, route db.query by SQL substring.
"""
from unittest.mock import AsyncMock, patch

import pytest
import routes.chat.sessions_list  # noqa: F401 — ensure submodule attr is set before patch()


def _query_router(routes):
    """Build an async db.query stub that returns the first matching canned result.

    routes: list of (sql_substring, result) — first substring match wins, so
    order specific fragments (e.g. the AI timestamp agg) before generic ones.
    """
    async def _q(sql, params=None):
        for needle, result in routes:
            if needle in sql:
                return result
        return []
    return _q


async def _list(rows, msgs, *, limit=200, offset=0):
    """Run list_sessions with pure-AI canned data (owner=None → no scope walk)."""
    db = AsyncMock()
    db.query = AsyncMock(side_effect=_query_router([
        ("SELECT * FROM chat_sessions", rows),
        ("FROM documents WHERE meta::id(id) IN", []),   # build_ref_map: no refs
        ("GROUP BY chat_id", msgs),  # AI last-msg aggregate (per-chat max created_at)
    ]))
    with patch("routes.chat.sessions_list.get_document_access", AsyncMock(return_value="full")), \
         patch("routes.chat.sessions_list.get_db", AsyncMock(return_value=db)), \
         patch("routes.chat.sessions_list.fetch_one", AsyncMock(return_value=None)):
        return await routes.chat.sessions_list.list_sessions(db=await routes.chat.sessions_list.get_db(), 
            project_id="p1", document_id="doc-1",
            limit=limit, offset=offset, user={"user_id": "u1"},
        )


def _ai_row(sid, *, updated, title="T", created=None):
    return {
        "id": sid, "is_note": False, "document_id": "doc-1",
        "title": title,
        "updated_at": updated, "created_at": created or updated,
    }


@pytest.mark.asyncio
async def test_orders_by_last_message_time_not_updated_at():
    """An AI chat with the OLDEST updated_at but NEWEST message sorts FIRST."""
    # DB ORDER BY updated_at DESC would return [s3, s2, s1] (the input order).
    rows = [
        _ai_row("s3", updated="2026-01-03T00:00:00Z"),
        _ai_row("s2", updated="2026-01-02T00:00:00Z"),
        _ai_row("s1", updated="2026-01-01T00:00:00Z"),
    ]
    # Aggregate result (GROUP BY chat_id → one row per chat, max created_at as last_at).
    msgs = [
        {"chat_id": "s3", "last_at": "2026-01-10T00:00:00Z"},
        {"chat_id": "s2", "last_at": "2026-01-11T00:00:00Z"},
        {"chat_id": "s1", "last_at": "2026-01-12T00:00:00Z"},
    ]
    out = await _list(rows, msgs)
    assert [s["session_id"] for s in out] == ["s1", "s2", "s3"]


@pytest.mark.asyncio
async def test_last_message_at_present_on_ai_session():
    """last_message_at is serialized for AI chats and equals the aggregate last_at."""
    rows = [_ai_row("s1", updated="2026-01-01T00:00:00Z")]
    # One aggregated row per chat (GROUP BY result): max created_at as last_at.
    msgs = [{"chat_id": "s1", "last_at": "2026-01-12T08:30:00Z"}]
    out = await _list(rows, msgs)
    assert out[0]["last_message_at"] == "2026-01-12T08:30:00Z"


@pytest.mark.asyncio
async def test_empty_chat_falls_back_to_updated_at():
    """A chat absent from the aggregate (no messages) sorts by updated_at, last_message_at=None."""
    rows = [
        _ai_row("sA", updated="2026-02-01T00:00:00Z"),   # empty → no agg row
        _ai_row("sB", updated="2026-01-01T00:00:00Z"),   # has a newer message
    ]
    msgs = [{"chat_id": "sB", "last_at": "2026-03-01T00:00:00Z"}]
    out = await _list(rows, msgs)
    # sB's last message (Mar) is newer than sA's updated_at (Feb) → sB first.
    assert [s["session_id"] for s in out] == ["sB", "sA"]
    sa = next(s for s in out if s["session_id"] == "sA")
    assert sa["last_message_at"] is None


@pytest.mark.asyncio
async def test_pagination_slices_sorted_list():
    """offset/limit apply to the SORTED list, not the raw DB order."""
    rows = [
        _ai_row("s3", updated="2026-01-03T00:00:00Z"),
        _ai_row("s2", updated="2026-01-02T00:00:00Z"),
        _ai_row("s1", updated="2026-01-01T00:00:00Z"),
    ]
    msgs = [
        {"chat_id": "s3", "last_at": "2026-01-10T00:00:00Z"},
        {"chat_id": "s2", "last_at": "2026-01-11T00:00:00Z"},
        {"chat_id": "s1", "last_at": "2026-01-12T00:00:00Z"},
    ]
    # Sorted = [s1, s2, s3]; offset=1, limit=1 → [s2].
    out = await _list(rows, msgs, limit=1, offset=1)
    assert [s["session_id"] for s in out] == ["s2"]


@pytest.mark.asyncio
async def test_ai_last_activity_aggregate_filters_user_role_only():
    """F3 (plan: chat-list-ux-and-sort-fixes): the AI last-activity aggregate
    scans ONLY user messages (role='user'), so last_message_at reflects the last
    USER message time — immutable across the assistant reply + the
    auto_title/update_session updated_at bumps. This pins the SQL decision: a
    regression that drops the role filter re-introduces the enter→leave reorder
    jump (assistant reply time / updated_at would reshuffle the list). The notes
    preview path stays last-message-of-any-role (out of scope, unchanged)."""
    seen_sql: list[str] = []

    async def _capture_query(sql, params=None):
        seen_sql.append(sql)
        if "SELECT * FROM chat_sessions" in sql:
            return [_ai_row("s1", updated="2026-01-01T00:00:00Z")]
        if "FROM documents WHERE meta::id(id) IN" in sql:
            return []
        # AI last-msg aggregate — return a user-message timestamp.
        if "GROUP BY chat_id" in sql:
            return [{"chat_id": "s1", "last_at": "2026-01-12T08:30:00Z"}]
        return []

    db = AsyncMock()
    db.query = AsyncMock(side_effect=_capture_query)
    with patch("routes.chat.sessions_list.get_document_access", AsyncMock(return_value="full")), \
         patch("routes.chat.sessions_list.get_db", AsyncMock(return_value=db)), \
         patch("routes.chat.sessions_list.fetch_one", AsyncMock(return_value=None)):
        out = await routes.chat.sessions_list.list_sessions(db=await routes.chat.sessions_list.get_db(), 
            project_id="p1", document_id="doc-1", limit=200, offset=0,
            user={"user_id": "u1"},
        )

    # The aggregate ran and surfaced the user-message time on the card.
    assert out[0]["last_message_at"] == "2026-01-12T08:30:00Z"
    # Pin the decision: the AI aggregate SQL MUST restrict to user messages.
    ai_agg_sql = next(s for s in seen_sql if "GROUP BY chat_id" in s)
    assert "role = 'user'" in ai_agg_sql, (
        "AI last-activity aggregate must filter role = 'user' (F3); got: " + ai_agg_sql
    )
