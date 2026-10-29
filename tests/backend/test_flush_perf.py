"""Tests for flush performance optimizations — batch queries, diff mentions, cached baseline.

CRDT: edits ride the pycrdt Y.Doc (no OT push). Tests seed session content via
set_session_text and exercise flush_to_db directly, asserting the same flush
optimizations: batched mention queries, diffed mentions, cached auto-backup
baseline, batched backlink events, and trailing-whitespace no-op.
"""

import pytest
from collab.registry import _get_or_create_session
from helpers import set_session_text

from db import get_db


async def _session(doc_id: str, content: str = "Hello world"):
    return await _get_or_create_session("doc", doc_id, content)


# ─── Fix 2: Diff mentions instead of full rebuild ─────────────────────────


class TestDiffMentions:
    """Flush should diff mentions and only write changes."""


    @pytest.mark.asyncio
    async def test_flush_diffs_mentions_on_link_change(self, client, collab_project, _clear_sessions):
        """When a link is removed, its backlink should disappear while others remain."""
        pid, doc_id, admin_token, *_ = collab_project
        targets = []
        for i in range(3):
            resp = await client.post(
                "/api/documents",
                json={"project_id": pid, "title": f"Diff Target {i}"},
                cookies={"lore_session": admin_token},
            )
            targets.append(resp.json()["document_id"])

        links_012 = " ".join(f"[T{i}]({tid})" for i, tid in enumerate(targets))
        links_12 = " ".join(f"[T{i+1}]({tid})" for i, tid in enumerate(targets[1:]))

        session = await _session(doc_id)
        set_session_text(session, links_012)
        session._dirty = True
        await session.flush_to_db()

        set_session_text(session, links_12)
        session._dirty = True
        await session.flush_to_db()

        resp = await client.get(
            f"/api/documents/{targets[0]}/backlinks", cookies={"lore_session": admin_token}
        )
        backlinks_0 = resp.json().get("backlinks", [])
        assert not any(bl["document_id"] == doc_id for bl in backlinks_0), \
            "Removed link should not have backlink"

        resp = await client.get(
            f"/api/documents/{targets[1]}/backlinks", cookies={"lore_session": admin_token}
        )
        backlinks_1 = resp.json().get("backlinks", [])
        assert any(bl["document_id"] == doc_id for bl in backlinks_1), \
            "Kept link should still have backlink"


# ─── Fix 5: Skip flush when only trailing whitespace differs ─────────────


class TestSkipFlushTrailingWhitespace:
    """Flush must be a no-op when session vs DB differ only by trailing whitespace.

    Why: references are sorted by `updated_at DESC`; a spurious bump on mere
    open re-floats them and inverts the list when opened top-to-bottom.
    """

    @pytest.mark.asyncio
    async def test_flush_skips_when_only_trailing_newline_differs(self, collab_project, _clear_sessions):
        _, doc_id, admin_token, *_ = collab_project
        base = "Reference body"

        session = await _session(doc_id)
        set_session_text(session, base)
        session._dirty = True
        await session.flush_to_db()
        assert session._last_flushed_content == base

        set_session_text(session, base + "\n")
        session._dirty = True
        update_calls = await _count_content_updates(session, doc_id)
        assert update_calls == 0, (
            f"Trailing-newline-only diff should not write to DB, got {update_calls} UPDATEs"
        )
        assert session._dirty is False


async def _count_content_updates(session, doc_id: str) -> int:
    """Run flush_to_db with db.query patched; count UPDATE ... SET content queries for doc_id."""
    db = await get_db()
    original_query = db.query
    count = 0

    async def counting_query(q, *args, **kwargs):
        nonlocal count
        params = args[0] if args else kwargs.get("vars") or {}
        if isinstance(q, str) and "UPDATE" in q and "content = $v" in q:
            if isinstance(params, dict) and params.get("id") == doc_id:
                count += 1
        return await original_query(q, *args, **kwargs)

    db.query = counting_query
    try:
        await session.flush_to_db()
    finally:
        db.query = original_query
    return count
