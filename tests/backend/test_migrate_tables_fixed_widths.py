"""The tables_fixed_widths migration: legacy 0 widths become birth-rule px, px columns stay."""

import secrets

import pytest
from pycrdt import Doc, Map

from migrations.migrate_tables_fixed_widths import run
from table_serialize import (
    _table_columns_and_rows,
    create_table_entry,
    ordered_axis_ids,
)


def _doc_with_widths(widths: list[int]) -> Doc:
    doc = Doc(client_id=7)
    create_table_entry(doc, "t1", [["#", "Description", "Notes"], ["1", "x", "a" * 30]])
    cols = doc.get("tables", type=Map)["t1"]["columns"]
    with doc.transaction():
        for cid, w in zip(ordered_axis_ids(cols), widths):
            cols[cid]["w"] = w
    return doc


# TEST-WHY: data-loss — the migration rewrites every shared table; px columns must survive byte-identical and a re-run must write nothing.
@pytest.mark.asyncio
async def test_run_freezes_zero_columns_only_and_reruns_as_noop(test_db, project_with_doc):
    import ydoc_store
    from db import create_record, get_db

    pid, host_id, _ = project_with_doc
    db = await get_db()
    doc_id = f"tfw-{secrets.token_hex(4)}"
    await create_record("documents", doc_id, {
        "project_id": pid, "parent_id": host_id, "title": doc_id, "content": "",
        "path": f"{doc_id}.md", "is_index": False, "is_reference": False,
    })
    await ydoc_store.append_update(doc_id, _doc_with_widths([0, 300, 0]).get_update())

    published: list[str] = []

    async def recorder(entity_id, update, *, append=True):
        published.append(entity_id)
        await ydoc_store.publish_doc_update(entity_id, update, append=append)

    dry = await run(db, dry_run=True, publish=recorder)
    assert published == [] and dry["columns"] >= 2

    await run(db, publish=recorder)
    assert published.count(doc_id) == 1
    widths, _ = _table_columns_and_rows((await ydoc_store.load(doc_id)).get("tables", type=Map)["t1"])
    assert widths == [49, 300, 495]

    await run(db, publish=recorder)
    assert published.count(doc_id) == 1
