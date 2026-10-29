"""Tests for the single document-creation entry point (documents.service).

Regression cover for the 2026-06-04 incident: documents created by non-endpoint
paths (extractor pipeline) skipped sort_key, leaving rows NONE → drag-reorder onto
such a neighbour returned 400 "after_id is not a sibling". The fix routes every
tree-document creation through `create_document`, which guarantees a sort_key.
"""

import uuid

import pytest


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.mark.asyncio
async def test_create_document_assigns_sort_key_for_tree_doc(project_with_doc):
    """A non-reference document created via the factory gets a sort_key."""
    from documents.service import create_document
    pid, _, _ = project_with_doc
    rec = await create_document(_uid(), {
        "project_id": pid, "parent_id": None, "title": "Extracted",
        "content": "x", "path": "x.md", "is_index": False,
    })
    assert rec.get("sort_key")


@pytest.mark.asyncio
async def test_create_document_prepends_newest_first(project_with_doc):
    """Two factory-created siblings: the second lands ABOVE the first (key is smaller)."""
    from documents.service import create_document
    pid, _, _ = project_with_doc
    first = await create_document(_uid(), {
        "project_id": pid, "parent_id": None, "title": "First",
        "content": "", "path": "a.md", "is_index": False,
    })
    second = await create_document(_uid(), {
        "project_id": pid, "parent_id": None, "title": "Second",
        "content": "", "path": "b.md", "is_index": False,
    })
    assert second["sort_key"] < first["sort_key"]


@pytest.mark.asyncio
async def test_reorder_after_factory_created_doc(client, admin_user, project_with_doc):
    """Regression: reorder onto a freshly factory-created sibling returns 200, not 400.

    Before the fix, a doc with sort_key NONE was filtered out of the sibling list,
    so using it as `after_id` produced a 400.
    """
    from documents.service import create_document
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    anchor_id, mover_id = _uid(), _uid()
    await create_document(anchor_id, {
        "project_id": pid, "parent_id": None, "title": "Anchor",
        "content": "", "path": "an.md", "is_index": False,
    })
    await create_document(mover_id, {
        "project_id": pid, "parent_id": None, "title": "Mover",
        "content": "", "path": "mv.md", "is_index": False,
    })
    resp = await client.patch(
        f"/api/documents/{mover_id}/reorder",
        json={"after_id": anchor_id},
        cookies=cookies,
    )
    assert resp.status_code == 200, resp.text


# --- create_reference_row: single creation chokepoint (C2) --------------------
#
# Pins the contract every reference-creation entry point delegates to: canonical
# dict shape, create_record insert, exactly one reference_created emit, and NO
# host resolution (the caller owns host policy — the collab path's "real host
# required" 400 depends on this).
