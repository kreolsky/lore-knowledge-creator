"""Backend-authoritative note-link strip on note delete (plan: note-link-delete-fix).

# ARCH: DELETE /sessions/{id} is the single source of truth for stripping a
# [label](note:<id>) anchor link from the owning entity's content. The strip runs
# in the existing fire-and-forget note-emit background task, so it is best-effort
# (note already soft-deleted) and never regresses DELETE latency.

These tests call `delete_session` directly with mocked DB + mocked convergence
helpers (`resolve_live_doc_state` / `route_document_content`), mirroring the
repo's mock-DB convention. The strip runs inside `fire_note_emit`, so each test
drains `_emit_tasks` before asserting.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import routes.chat.sessions  # noqa: F401 — ensure submodule attr is set before patch()


async def _drain_emit_tasks() -> None:
    from chat_sessions.note_events import _emit_tasks
    if _emit_tasks:
        await asyncio.gather(*_emit_tasks, return_exceptions=True)


async def _run_delete_with_content(session: dict, content: str) -> dict:
    captured: dict = {"live_content": content}

    async def fake_require(_sid, _user):
        return session

    db = AsyncMock()
    db.query = AsyncMock(return_value=[])

    async def fake_resolve(doc_id):
        captured["resolved_doc_id"] = doc_id
        return (captured["live_content"], "{}")

    async def fake_route(*, doc_id, new_content, project_id):
        captured["route"] = {"doc_id": doc_id, "new_content": new_content, "project_id": project_id}
        return False

    with patch("routes.chat.sessions._require_session_access", fake_require), \
         patch("routes.chat.sessions.get_db", AsyncMock(return_value=db)), \
         patch("chat_sessions.delete.resolve_live_doc_state", fake_resolve), \
         patch("chat_sessions.delete.route_document_content", fake_route):
        from routes.chat.sessions import delete_session
        await delete_session(session["id"], {"user_id": "u1"}, db=await routes.chat.sessions.get_db())
        await _drain_emit_tasks()
    return captured


@pytest.mark.asyncio
async def test_strips_link_label_preserved():
    """The label inside [label](note:id) is preserved; only the brackets + URL are removed."""
    session = {"id": "n2", "is_note": True, "document_id": "doc-b", "project_id": "p1"}
    captured = await _run_delete_with_content(session, "pre [My Label](note:n2) post")
    assert captured["route"]["new_content"] == "pre My Label post"


@pytest.mark.asyncio
async def test_strips_multiple_occurrences():
    """All occurrences of the same note id are stripped (re.sub is global)."""
    session = {"id": "n3", "is_note": True, "document_id": "doc-c", "project_id": "p1"}
    captured = await _run_delete_with_content(
        session, "[A](note:n3) mid [B](note:n3) end")
    assert captured["route"]["new_content"] == "A mid B end"


@pytest.mark.asyncio
async def test_strips_empty_label_link():
    """Empty-label [](note:id) links are fully removed (regex uses [^\\]]* not +)."""
    session = {"id": "n4", "is_note": True, "document_id": "doc-d", "project_id": "p1"}
    captured = await _run_delete_with_content(session, "x [](note:n4) y")
    assert captured["route"]["new_content"] == "x  y"


@pytest.mark.asyncio
async def test_anchorless_note_is_noop_on_content():
    """A note with no matching link in content → route_document_content NOT called."""
    session = {"id": "n5", "is_note": True, "document_id": "doc-e", "project_id": "p1"}
    captured = await _run_delete_with_content(session, "no link here")
    assert "route" not in captured


@pytest.mark.asyncio
async def test_reference_note_targets_reference_content_not_parent():
    """For a reference-note the strip MUST mutate session.document_id (the
    reference's own content), never the parent doc."""
    session = {"id": "n6", "is_note": True, "document_id": "ref-1", "project_id": "p1"}
    captured = await _run_delete_with_content(session, "[L](note:n6)")
    assert captured["route"]["doc_id"] == "ref-1"


@pytest.mark.asyncio
async def test_non_note_session_skips_strip():
    """A regular AI chat session (is_note != true) never touches document content."""
    session = {"id": "c1", "is_note": False, "document_id": "doc-x", "project_id": "p1"}
    captured = await _run_delete_with_content(session, "[L](note:c1)")
    assert "route" not in captured


@pytest.mark.asyncio
async def test_falsy_document_id_skips_strip():
    """Anchorless note with no document_id → no owning content → skip."""
    session = {"id": "n7", "is_note": True, "document_id": "", "project_id": "p1"}
    captured = await _run_delete_with_content(session, "[L](note:n7)")
    assert "route" not in captured


@pytest.mark.asyncio
async def test_strip_failure_does_not_break_delete():
    """A strip failure is best-effort — the DELETE already succeeded (soft-delete);
    a leftover link is inert. route raising must not propagate."""
    session = {"id": "n8", "is_note": True, "document_id": "doc-z", "project_id": "p1"}

    async def fake_require(_sid, _user):
        return session

    db = AsyncMock()
    db.query = AsyncMock(return_value=[])

    async def fake_resolve(_doc_id):
        return ("[L](note:n8)", "{}")

    call_count = {"n": 0}

    async def fake_route(*, doc_id, new_content, project_id):
        call_count["n"] += 1
        raise RuntimeError("boom")

    with patch("routes.chat.sessions._require_session_access", fake_require), \
         patch("routes.chat.sessions.get_db", AsyncMock(return_value=db)), \
         patch("chat_sessions.delete.resolve_live_doc_state", fake_resolve), \
         patch("chat_sessions.delete.route_document_content", fake_route):
        from routes.chat.sessions import delete_session
        # Must not raise.
        await delete_session("n8", {"user_id": "u1"}, db=await routes.chat.sessions.get_db())
        await _drain_emit_tasks()

    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_pattern_helper_strips_globally():
    from chat_sessions.delete import _note_link_pattern
    p = _note_link_pattern("abc-1")
    assert p.sub(r"\1", "[x](note:abc-1) [y](note:abc-1)") == "x y"
    # id-escape: a sibling id differing only by suffix must NOT match.
    p2 = _note_link_pattern("abc")
    assert p2.sub(r"\1", "[x](note:abc-1)") == "[x](note:abc-1)"
