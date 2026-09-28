"""Tests for the single document-creation entry point (documents.service).

Regression cover for the 2026-06-04 incident: documents created by non-endpoint
paths (extractor pipeline) skipped sort_key, leaving rows NONE → drag-reorder onto
such a neighbour returned 400 "after_id is not a sibling". The fix routes every
tree-document creation through `create_document`, which guarantees a sort_key.
"""

import uuid

import pytest
from emit_recorder import EmitRecorder


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
async def test_create_document_reference_skips_sort_key(project_with_doc):
    """Reference documents do not participate in reorder → no sort_key assigned."""
    from documents.service import create_document
    pid, idx_id, _ = project_with_doc
    rec = await create_document(_uid(), {
        "project_id": pid, "parent_id": idx_id, "title": "Ref",
        "content": "", "path": "_ref/r.md", "is_index": False,
        "is_reference": True, "media_type": "markdown", "source_url": "",
    })
    assert rec.get("sort_key") is None


@pytest.mark.asyncio
async def test_create_document_respects_explicit_sort_key(project_with_doc):
    """An explicitly provided sort_key is preserved (factory does not overwrite)."""
    from documents.service import create_document
    pid, _, _ = project_with_doc
    rec = await create_document(_uid(), {
        "project_id": pid, "parent_id": None, "title": "Fixed",
        "content": "", "path": "f.md", "is_index": False, "sort_key": "m",
    })
    assert rec["sort_key"] == "m"


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


@pytest.mark.asyncio
async def test_backfill_assigns_remaining_none(project_with_doc, test_db):
    """The unguarded sweep in assign_sort_keys_to_none_rows assigns keys to any leftover NONE rows."""
    from sort_keys import assign_sort_keys_to_none_rows

    from db import create_record
    pid, _, _ = project_with_doc
    orphan_id = _uid()
    # Simulate a legacy/orphaned tree doc with no sort_key.
    await create_record("documents", orphan_id, {
        "project_id": pid, "parent_id": None, "title": "Orphan",
        "content": "", "path": "orphan.md", "is_index": False, "sort_key": None,
    })
    await assign_sort_keys_to_none_rows(test_db)
    row = await test_db.query(
        "SELECT sort_key FROM type::record('documents', $id)", {"id": orphan_id}
    )
    assert row and row[0].get("sort_key")


# --- create_reference_row: single creation chokepoint (C2) --------------------
#
# Pins the contract every reference-creation entry point delegates to: canonical
# dict shape, create_record insert, exactly one reference_created emit, and NO
# host resolution (the caller owns host policy — the collab path's "real host
# required" 400 depends on this).


@pytest.mark.asyncio
async def test_create_reference_row_canonical_dict_and_emit(project_with_doc):
    """Core dict + insert + single emit; optionals omitted when not passed."""

    from documents.service import create_reference_row

    pid, idx_id, _ = project_with_doc
    ref_id = _uid()
    with EmitRecorder.active() as emit_mock:
        record = await create_reference_row(
            ref_id=ref_id, project_id=pid, host_id=idx_id,
            title="Plain Ref", media_type="markdown", content="hi",
        )
    # (a) canonical core dict
    assert record["path"] == f"_ref/{ref_id}.md"
    assert record["is_index"] is False
    assert record["is_reference"] is True
    assert record["parent_id"] == idx_id
    assert record["title"] == "Plain Ref"
    assert record["media_type"] == "markdown"
    assert record["content"] == "hi"
    for absent in ("processing_status", "file_path", "file_meta", "source_url",
                   "created_by", "created_by_name"):
        assert absent not in record, f"{absent} must be absent without an explicit value"
    # (b) inserted via create_record (row persists)
    from db import fetch_one

    persisted = await fetch_one("documents", ref_id)
    assert persisted is not None
    assert persisted["is_reference"] is True
    # (c) exactly one reference_created emit with the canonical payload. The
    # primitive ALSO emits content_flushed for a body-carrying create (D1, plan
    # 1786840000000) — the reference_created count is what this pins, not the
    # total emit count.
    ref_created = emit_mock.of("reference_created")
    assert len(ref_created) == 1
    assert ref_created[0] == {
        "project_id": pid, "reference_id": ref_id,
        "title": "Plain Ref", "document_id": idx_id,
        # Author fields always ride the emit (None when no creator — impersonal/widget
        # paths); the project-WS allowlist whitelists them so a client shows the nick
        # without a reload.
        "created_by": None, "created_by_name": None,
    }


@pytest.mark.asyncio
async def test_create_reference_row_passes_optional_fields(project_with_doc):
    """Optional fields are written ONLY when a non-None value is passed."""
    from documents.service import create_reference_row

    pid, idx_id, _ = project_with_doc
    ref_id = _uid()
    record = await create_reference_row(
        ref_id=ref_id, project_id=pid, host_id=idx_id,
        title="Img", media_type="image",
        processing_status="ready", file_path="p/r.png",
        file_meta={"mime_type": "image/png"}, source_url="https://x",
    )
    assert record["processing_status"] == "ready"
    assert record["file_path"] == "p/r.png"
    assert record["file_meta"] == {"mime_type": "image/png"}
    assert record["source_url"] == "https://x"


@pytest.mark.asyncio
async def test_create_reference_row_takes_host_verbatim(project_with_doc):
    """The service does NOT resolve/normalize the host — caller owns host policy.

    Passing a real document (not the project index doc) as host_id, the created
    reference's parent_id must equal it verbatim. Guards the collab path's "real
    host required" semantics: a service that normalized empty→index would let
    agent-created refs silently re-host on the index doc.
    """
    from documents.service import create_document, create_reference_row

    pid, idx_id, _ = project_with_doc
    host_id = _uid()
    await create_document(host_id, {
        "project_id": pid, "parent_id": None, "title": "Host",
        "content": "", "path": "host.md", "is_index": False,
    })
    ref_id = _uid()
    record = await create_reference_row(
        ref_id=ref_id, project_id=pid, host_id=host_id,
        title="R", media_type="markdown",
    )
    assert record["parent_id"] == host_id
    assert record["parent_id"] != idx_id
