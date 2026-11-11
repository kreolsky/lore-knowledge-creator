"""The tables_v2 migration: a v1 (positional) table becomes v2 (id-keyed) under the same id,
text/widths/label carried, a note inside a cell still quoting the same text — and a
document whose cell note cannot be carried keeps v1 untouched."""

import secrets

import pytest
from note_anchors import utf16_to_cp
from pycrdt import Array, Assoc, Doc, Map, StickyIndex, Text

from migrations._shared import MigrationDeferred
from migrations.migrate_tables_v2 import (
    CellNote,
    _char_ids,
    _item_id,
    _shadow,
    migrate_doc_tables,
    run,
)
from table_serialize import (
    TABLE_SCHEMA_VERSION,
    apply_tables_json,
    capture_tables_json,
    cell_key,
    ordered_ids,
    table_awaits_migration,
    table_model_from_map,
)


def _v1_doc(matrix: list[list[str]], widths: list[int], *, label: str | None = None) -> Doc:
    """A document holding table `t1` in the v1 shape; cell (1, 0) gets a second text item."""
    doc = Doc(client_id=7)
    doc.get("content", type=Text).insert(0, "до 😀 ![T](table:t1) после")
    tables = doc.get("tables", type=Map)
    with doc.transaction():
        table = Map()
        tables["t1"] = table
        cols = Array()
        table["columns"] = cols
        for w in widths:
            col = Map()
            cols.append(col)
            col["w"] = w
        rows = Array()
        table["rows"] = rows
        for row in matrix:
            yrow = Array()
            rows.append(yrow)
            for text in row:
                cell = Map()
                yrow.append(cell)
                cell["t"] = Text(text)
        if label is not None:
            table["label"] = label
    tables["t1"]["rows"][1][0]["t"].insert(len("а".encode()), "Жв")  # «аЖвб😀»: a second item mid-text
    return doc


def _cell_anchor(doc: Doc, r: int, c: int, start: int, end: int) -> tuple[bytes, bytes]:
    """A v1 cell anchor over UTF-16 [start, end), built the way the browser builds it."""
    shadow, _ = _shadow(doc)
    text = shadow.get("tables", type=Map)["t1"]["rows"][r][c]["t"]
    return (
        StickyIndex.new(text, start, Assoc.AFTER).encode(),
        StickyIndex.new(text, end, Assoc.BEFORE).encode(),
    )


def _v2_quote(doc: Doc, r: int, c: int, rel_start: bytes, rel_end: bytes) -> str:
    """The text a v2 cell anchor covers, asserting both halves bind inside cell (r, c)."""
    shadow, _ = _shadow(doc)
    table = shadow.get("tables", type=Map)["t1"]
    row_ids, col_ids = ordered_ids(table)
    key = cell_key(row_ids[r], col_ids[c])
    chars = _char_ids(shadow, {key: table["cells"][key]})
    (ks, s), (ke, e) = chars[_item_id(rel_start)], chars[_item_id(rel_end)]
    assert ks == ke == key
    real = str(doc.get("tables", type=Map)["t1"]["cells"][key])
    return real[utf16_to_cp(real, s):utf16_to_cp(real, e + 1)]


# TEST-WHY: data-loss — a v1 table and a note inside its cell must survive the rebuild unchanged.
def test_v1_table_becomes_v2_with_text_widths_label_and_cell_note_carried():
    doc = _v1_doc([["Имя", "😀"], ["аб😀"], ["x", "y", "лишняя"]], [120, 200], label="Сводка")
    # «Жвб😀» across both items of cell (1, 0): UTF-16 [1, 6).
    rs, re_ = _cell_anchor(doc, 1, 0, 1, 6)
    host = StickyIndex.new(doc.get("content", type=Text), 0, Assoc.AFTER).encode()
    notes = [CellNote("n1", rs, re_), CellNote("host", host, host)]
    peer = Doc()
    peer.apply_update(doc.get_update())

    result = migrate_doc_tables(doc, "d1", notes)
    assert result.deferred is None and result.tables == 1 and result.cell_notes == 1
    assert table_awaits_migration(doc.get("tables", type=Map)["t1"])  # pure: input untouched
    peer.apply_update(result.update)

    table = peer.get("tables", type=Map)["t1"]
    assert table["v"] == TABLE_SCHEMA_VERSION and table["label"] == "Сводка"
    assert table_model_from_map(table) == [["Имя", "😀", ""], ["аЖвб😀", "", ""], ["x", "y", "лишняя"]]
    assert capture_tables_json(peer) == (
        '{"t1":{"columns":[120,200,160],"rows":[["Имя","😀",""],["аЖвб😀","",""],["x","y","лишняя"]]}}'
    )
    assert set(result.anchors) == {"n1"}
    assert _v2_quote(peer, 1, 0, *result.anchors["n1"]) == "Жвб😀"
    # Deterministic: a crashed run's re-run mints byte-identical items and anchors.
    again = migrate_doc_tables(doc, "d1", notes)
    assert again.update == result.update and again.anchors == result.anchors
    # A crashed run's notes (already rebound, rebuild not published) verify, not defer.
    rerun = migrate_doc_tables(doc, "d1", [CellNote("n1", *result.anchors["n1"])])
    assert rerun.deferred is None and rerun.cell_notes == 1 and rerun.anchors == {}
    assert migrate_doc_tables(peer, "d1", notes).tables == 0


# TEST-WHY: data-loss — an anchor that cannot be carried must keep its document v1, never drop it.
def test_unrebindable_cell_note_defers_and_v1_table_is_never_captured_empty():
    doc = _v1_doc([["a", "b"], ["аб😀", "d"]], [100, 100])
    start, _ = _cell_anchor(doc, 0, 0, 0, 1)
    _, end = _cell_anchor(doc, 0, 1, 0, 1)  # straddles cells (0, 0) → (0, 1)

    result = migrate_doc_tables(doc, "d1", [CellNote("n1", start, end)])
    assert result.deferred and "1 of 1" in result.deferred
    assert result.update is None and result.anchors == {}

    # A deferred document under v2 code: capture omits the v1 table, a restore keeps it.
    assert capture_tables_json(doc) == "{}"
    apply_tables_json(doc, capture_tables_json(doc))
    assert table_awaits_migration(doc.get("tables", type=Map)["t1"])


# TEST-WHY: data-loss — note rows are written before the rebuild is published, and a re-run is a no-op.
@pytest.mark.asyncio
async def test_run_rebinds_before_publishing_defers_the_rest_and_reruns_idempotently(
    test_db, project_with_doc,
):
    import ydoc_store
    from db import create_record, fetch_one, get_db

    pid, host_id, admin_uid = project_with_doc
    db = await get_db()
    good, bad = (f"tv2-{secrets.token_hex(4)}" for _ in range(2))
    seeds = {good: _v1_doc([["h"], ["аб😀"]], [90]), bad: _v1_doc([["a", "b"], ["аб😀", "d"]], [90, 90])}
    anchors = {good: _cell_anchor(seeds[good], 1, 0, 1, 6), bad: (
        _cell_anchor(seeds[bad], 0, 0, 0, 1)[0], _cell_anchor(seeds[bad], 0, 1, 0, 1)[1],
    )}
    for doc_id, seed in seeds.items():
        await create_record("documents", doc_id, {
            "project_id": pid, "parent_id": host_id, "title": doc_id, "content": "",
            "path": f"{doc_id}.md", "is_index": False, "is_reference": False,
        })
        await ydoc_store.append_update(doc_id, seed.get_update())
        await create_record("chat_sessions", f"n-{doc_id}", {
            "project_id": pid, "document_id": doc_id, "user_id": admin_uid, "is_note": True,
        })
        await db.query(
            "UPDATE type::record('chat_sessions', $id) SET anchor_rel_start = $rs, anchor_rel_end = $re",
            {"id": f"n-{doc_id}", "rs": anchors[doc_id][0], "re": anchors[doc_id][1]},
        )

    published: list[tuple[str, dict]] = []

    async def recorder(entity_id, update, *, append=True):
        published.append((entity_id, await fetch_one("chat_sessions", f"n-{entity_id}")))
        await ydoc_store.publish_doc_update(entity_id, update, append=append)

    stats = await run(db, dry_run=True, publish=recorder)
    assert published == [] and stats["deferred"] >= 1

    with pytest.raises(MigrationDeferred):
        await run(db, publish=recorder)
    assert [e for e, _ in published] == [good]
    assert published[0][1]["anchor_rel_start"] != anchors[good][0]  # rebound BEFORE publish
    loaded = await ydoc_store.load(good)
    row = await fetch_one("chat_sessions", f"n-{good}")
    assert _v2_quote(loaded, 1, 0, row["anchor_rel_start"], row["anchor_rel_end"]) == "Жвб😀"
    assert table_awaits_migration((await ydoc_store.load(bad)).get("tables", type=Map)["t1"])

    with pytest.raises(MigrationDeferred):
        await run(db, publish=recorder)
    assert len(published) == 1
