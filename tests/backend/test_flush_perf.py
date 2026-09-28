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


# ─── Fix 1: Batch RELATE into single query ────────────────────────────────


class TestBatchMentionQueries:
    """Flush should batch DELETE + RELATE into minimal DB roundtrips."""

    @pytest.mark.asyncio
    async def test_flush_batches_mention_queries(self, collab_project, _clear_sessions):
        """A doc with 5 mentions should flush with a small number of DB queries."""
        pid, doc_id, admin_token, *_ = collab_project
        target_ids = [await _create_target_doc(pid, f"Target {i}") for i in range(5)]
        links = " ".join(f"[T{i}]({tid})" for i, tid in enumerate(target_ids))

        session = await _session(doc_id)
        set_session_text(session, links)
        session._dirty = True

        query_count = 0
        db = await get_db()
        original_query = db.query

        async def counting_query(*args, **kwargs):
            nonlocal query_count
            query_count += 1
            return await original_query(*args, **kwargs)

        db.query = counting_query
        try:
            await session.flush_to_db()
        finally:
            db.query = original_query

        assert query_count <= 8, f"Expected <=8 DB queries with batching, got {query_count}"

    @pytest.mark.asyncio
    async def test_rebuild_doc_mentions_helper_creates_edges(self, client, collab_project):
        """The shared rebuild_doc_mentions helper creates correct edges."""
        pid, doc_id, admin_token, *_ = collab_project
        resp = await client.post(
            "/api/documents",
            json={"project_id": pid, "title": "Link Target"},
            cookies={"lore_session": admin_token},
        )
        target_id = resp.json()["document_id"]

        from mentions import rebuild_doc_mentions
        db = await get_db()
        content = f"See [Link Target]({target_id})"
        mentioned = await rebuild_doc_mentions(db, "documents", doc_id, content)

        assert target_id in mentioned
        rows = await db.query(
            "SELECT * FROM doc_mentions WHERE in = type::record('documents', $id)",
            {"id": doc_id},
        )
        assert len(rows) >= 1


# ─── Fix 2: Diff mentions instead of full rebuild ─────────────────────────


class TestDiffMentions:
    """Flush should diff mentions and only write changes."""

    @pytest.mark.asyncio
    async def test_flush_skips_mentions_when_unchanged(self, collab_project, _clear_sessions):
        """Second flush with same links should not execute any mention queries."""
        pid, doc_id, admin_token, *_ = collab_project
        target_id = await _create_target_doc(pid)

        session = await _session(doc_id)
        set_session_text(session, f"See [T]({target_id})")
        session._dirty = True
        await session.flush_to_db()

        # Text-only edit (no link changes)
        set_session_text(session, f"Prefix: See [T]({target_id})")
        session._dirty = True

        mention_queries = 0
        db = await get_db()
        original_query = db.query

        async def tracking_query(query_str, *args, **kwargs):
            nonlocal mention_queries
            if isinstance(query_str, str) and ("doc_mentions" in query_str or "RELATE" in query_str):
                mention_queries += 1
            return await original_query(query_str, *args, **kwargs)

        db.query = tracking_query
        try:
            await session.flush_to_db()
        finally:
            db.query = original_query

        assert mention_queries == 0, (
            f"Expected 0 mention queries when links unchanged, got {mention_queries}"
        )

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


# ─── Fix 3: Cached baseline for auto_backup ───────────────────────────────


class TestCachedBaseline:
    """Auto-backup should use cached content instead of a DB read."""

    @pytest.mark.asyncio
    async def test_auto_backup_uses_cached_baseline(self, collab_project, _clear_sessions):
        """After flush, session tracks _last_flushed_content for the auto_backup baseline."""
        _, doc_id, admin_token, *_ = collab_project
        big_content = "A" * 500

        session = await _session(doc_id)
        set_session_text(session, big_content)
        session._dirty = True
        await session.flush_to_db()

        assert session._last_flushed_content == big_content

    @pytest.mark.asyncio
    async def test_auto_backup_no_db_read_on_second_flush(self, collab_project, _clear_sessions):
        """Second flush uses the cached baseline (_last_flushed_content), not a doc re-read."""
        _, doc_id, admin_token, *_ = collab_project

        session = await _session(doc_id)
        set_session_text(session, "A" * 500)
        session._dirty = True
        await session.flush_to_db()

        set_session_text(session, "small")
        session._dirty = True

        fetch_calls = []
        import routes.documents as docs_mod
        orig_fetch_one = docs_mod.fetch_one

        async def tracking_fetch(table, rid):
            if table == "documents" and rid == doc_id:
                fetch_calls.append((table, rid))
            return await orig_fetch_one(table, rid)

        docs_mod.fetch_one = tracking_fetch
        try:
            await session.flush_to_db()
        finally:
            docs_mod.fetch_one = orig_fetch_one

        assert len(fetch_calls) == 0, (
            f"Expected 0 DB reads for baseline (should use cache), got {len(fetch_calls)}"
        )


# ─── Fix 4: Batch backlinks_changed events ────────────────────────────────


class TestBatchBacklinksEvents:
    """Backlink change notifications should be batched into a single event."""

    @pytest.mark.asyncio
    async def test_backlinks_changed_batch_event(self, collab_project, _clear_sessions):
        """Flush with 3 new mentions should emit one batched event, not 3 separate ones."""
        pid, doc_id, admin_token, *_ = collab_project
        targets = [await _create_target_doc(pid, f"Event Target {i}") for i in range(3)]
        links = " ".join(f"[T{i}]({tid})" for i, tid in enumerate(targets))

        session = await _session(doc_id)
        set_session_text(session, links)
        session._dirty = True

        emitted_events = []
        import event_bus
        original_emit = event_bus.emit

        async def tracking_emit(event_type, **kwargs):
            emitted_events.append((event_type, kwargs))
            return await original_emit(event_type, **kwargs)

        event_bus.emit = tracking_emit
        try:
            await session.flush_to_db()
        finally:
            event_bus.emit = original_emit

        backlink_events = [e for e in emitted_events if "backlinks" in e[0]]
        assert len(backlink_events) == 1, (
            f"Expected 1 batched backlinks event, got {len(backlink_events)}: {backlink_events}"
        )
        event_type, event_kwargs = backlink_events[0]
        assert event_type == "backlinks_changed_batch"
        assert set(event_kwargs["document_ids"]) == set(targets)


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

    @pytest.mark.asyncio
    async def test_flush_skips_when_only_trailing_spaces_differ(self, collab_project, _clear_sessions):
        _, doc_id, admin_token, *_ = collab_project
        base = "Body text"

        session = await _session(doc_id)
        set_session_text(session, base)
        session._dirty = True
        await session.flush_to_db()

        set_session_text(session, base + "   \t\n")
        session._dirty = True
        update_calls = await _count_content_updates(session, doc_id)
        assert update_calls == 0
        assert session._dirty is False

    @pytest.mark.asyncio
    async def test_flush_still_persists_on_real_content_change(self, collab_project, _clear_sessions):
        _, doc_id, admin_token, *_ = collab_project
        base = "Body text"

        session = await _session(doc_id)
        set_session_text(session, base)
        session._dirty = True
        await session.flush_to_db()

        set_session_text(session, "Body texT")  # non-trailing change
        session._dirty = True
        update_calls = await _count_content_updates(session, doc_id)
        assert update_calls >= 1, "Real content change must persist"


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


# ─── Helpers ──────────────────────────────────────────────────────────────


async def _create_target_doc(project_id: str, title: str = "Target") -> str:
    """Create a minimal target document for linking tests."""
    from uuid import uuid4
    db = await get_db()
    tid = str(uuid4())
    await db.query(
        "CREATE type::record('documents', $id) SET project_id = $pid, title = $t, "
        "path = $p, content = '', deleted_at = NONE",
        {"id": tid, "pid": project_id, "t": title, "p": f"/{tid}.md"},
    )
    return tid
