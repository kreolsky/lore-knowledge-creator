"""Migration index_children_to_root — documents under their project's index
doc move to the project root; references are carved out.

The index document is not a tree parent (the tree renders its row childless),
so a document stored under it exists but is invisible. Writers now normalise
index → root for documents (documents.service.effective_parent_id); this
migration covers pre-existing rows:
  - a document under the index ⇒ parent_id = NONE (sort_key kept, no
    updated_at bump — position is not an edit);
  - a REFERENCE under the index ⇒ untouched (the index is the canonical
    project-level host, documents_reference_parent_check);
  - idempotent: a second run updates nothing.
"""

import pytest

from db import create_record


async def _row(test_db, rid):
    rows = await test_db.query(
        "SELECT parent_id, sort_key, updated_at FROM type::record('documents', $id)",
        {"id": rid},
    )
    return rows[0]


@pytest.mark.asyncio
async def test_migration_moves_index_children_to_root(test_db, project_with_doc):
    pid, idx_id, _ = project_with_doc

    doc = "mig-idx-child"
    await create_record("documents", doc, {
        "project_id": pid, "parent_id": idx_id, "title": "Legacy under index",
        "content": "", "path": "mig-idx-child.md", "is_index": False,
        "sort_key": "k9",
    })
    # Pin updated_at — the migration must not bump it.
    await test_db.query(
        "UPDATE type::record('documents', $id) SET updated_at = <datetime>$t",
        {"id": doc, "t": "2026-01-01T00:00:00Z"},
    )
    ref = "mig-idx-ref"
    await create_record("documents", ref, {
        "project_id": pid, "parent_id": idx_id, "title": "Project-level ref",
        "content": "", "path": f"_ref/{ref}.md", "is_index": False,
        "is_reference": True, "media_type": "markdown",
    })

    from migrations.migrate_index_children_to_root import (
        _migrate_index_children_to_root,
    )

    await _migrate_index_children_to_root(test_db)

    doc_row = await _row(test_db, doc)
    assert doc_row.get("parent_id") is None, "doc under the index lands at root"
    assert doc_row.get("sort_key") == "k9", "sort_key is kept"
    assert str(doc_row.get("updated_at")).startswith("2026-01-01"), \
        "position is not an edit — no updated_at bump"

    ref_row = await _row(test_db, ref)
    assert ref_row.get("parent_id") == idx_id, \
        "a reference under the index keeps its canonical host"

    # Second run: idempotent — nothing left to move.
    await _migrate_index_children_to_root(test_db)
    assert (await _row(test_db, doc)).get("parent_id") is None
    assert (await _row(test_db, ref)).get("parent_id") == idx_id
