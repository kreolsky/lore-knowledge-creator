"""No-session surgical edits must not double-materialize the Y.Doc.

Bug (found by live smoke 2026-07-09): on a document WITHOUT ydoc_state, the
no-session branch of route_document_edit seeds a Y.Doc from documents.content,
splices, and publishes doc.get_update() — the FULL state including the seed —
into the ydoc_updates log. The NEXT no-session edit seeds a fresh Y.Doc from the
now-updated documents.content (a different materialization of the same text) and
replays the log on top; the CRDT merge of two parallel materializations
duplicates/loses text ("Alpha beta gamma" → "Alpha [S4] gammagamma").

These tests drive the real store (no mocks): two sequential edits must yield the
correct text, and load() must reconstruct exactly what documents.content says.
"""

import pytest
from collab.registry import _session_key, _sessions

from db import get_db


async def _db_content(doc_id: str) -> str:
    db = await get_db()
    rows = await db.query(
        "SELECT content FROM type::record('documents', $id)", {"id": doc_id}
    )
    return rows[0]["content"]


async def _edit(doc_id: str, project_id: str, old: str, new: str) -> None:
    """Resolve offsets against the live doc state and splice — the exact call
    sequence _apply_edit_proposal makes, minus HTTP, driven through the BATCH
    convergence core (route_document_edits) the edit path delegates to."""
    from agent.doc_state import (
        resolve_live_doc_state,
        route_document_edits,
    )
    from agent.edit_primitives import resolve_edit_range

    import config

    content, _ = await resolve_live_doc_state(doc_id)
    rng = resolve_edit_range(
        content, old, full_rewrite_fraction=config.AGENT_FULL_REWRITE_FRACTION,
    )
    assert not isinstance(rng, str), f"resolver failed: {rng} (content={content!r})"
    from_cp, to_cp, _folded = rng
    await route_document_edits(
        doc_id=doc_id,
        edits=[{
            "from_cp": from_cp,
            "to_cp": to_cp,
            "new_text": new,
            "original_text": content[from_cp:to_cp],
        }],
        project_id=project_id,
    )


@pytest.mark.asyncio
async def test_two_sequential_no_session_edits_do_not_corrupt(collab_project):
    """Second no-session edit on a fresh (no ydoc_state) doc keeps prior text intact."""
    pid, doc_id, *_ = collab_project
    _sessions.pop(_session_key("doc", doc_id), None)

    await _edit(doc_id, pid, "Hello", "Hello [one]")
    assert await _db_content(doc_id) == "Hello [one] world"

    _sessions.pop(_session_key("doc", doc_id), None)
    await _edit(doc_id, pid, "world", "world [two]")
    assert await _db_content(doc_id) == "Hello [one] world [two]"


@pytest.mark.asyncio
async def test_load_reconstructs_db_content_after_no_session_edit(collab_project):
    """After a no-session edit, load() (snapshot + log replay) must equal the
    derived documents.content — a diverged reconstruction is the corruption seed."""
    from ydoc_store import get_text, load

    pid, doc_id, *_ = collab_project
    _sessions.pop(_session_key("doc", doc_id), None)

    await _edit(doc_id, pid, "world", "world!")
    db_content = await _db_content(doc_id)
    assert db_content == "Hello world!"

    reconstructed = str(get_text(await load(doc_id)))
    assert reconstructed == db_content
