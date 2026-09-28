"""`expand_tables(doc)` — derived-plaintext expansion of table anchors (Order step 6).

Builds a pycrdt Doc with the `tables` subtree shape and asserts each `![label](table:id)`
anchor in `content` expands to a GFM table, splicing at the anchor offset.
"""
import pycrdt as Y

from table_serialize import expand_tables, table_model_from_map


def _make_table(doc: Y.Doc, tid: str, matrix: list[list[str]]) -> None:
    tables = doc.get("tables", type=Y.Map)
    table = Y.Map()
    tables[tid] = table
    cols = Y.Array()
    table["columns"] = cols
    width = max(len(r) for r in matrix)
    for _ in range(width):
        c = Y.Map()
        cols.append(c)
        c["w"] = 160
    rows = Y.Array()
    table["rows"] = rows
    for row in matrix:
        yrow = Y.Array()
        rows.append(yrow)
        for i in range(width):
            cell = Y.Map()
            yrow.append(cell)
            cell["t"] = Y.Text(row[i] if i < len(row) else "")


def test_model_from_map_round_trips():
    doc = Y.Doc()
    _make_table(doc, "t1", [["a", "b"], ["c", "d"]])
    tables = doc.get("tables", type=Y.Map)
    assert table_model_from_map(tables["t1"]) == [["a", "b"], ["c", "d"]]


def test_expand_replaces_anchor_with_gfm():
    doc = Y.Doc()
    text = doc.get("content", type=Y.Text)
    text += "intro\n\n![Cast](table:t1)\n\nouter"
    _make_table(doc, "t1", [["Name", "Role"], ["Red", "Hero"]])

    out = expand_tables(doc)
    assert "| Name | Role |" in out
    assert "| --- | --- |" in out
    assert "| Red | Hero |" in out
    # Surrounding text intact.
    assert out.startswith("intro\n\n")
    assert out.endswith("\n\nouter")
    # Anchor is gone.
    assert "table:t1" not in out


def test_orphan_anchor_left_verbatim():
    doc = Y.Doc()
    text = doc.get("content", type=Y.Text)
    text += "x ![missing](table:gone) y"
    out = expand_tables(doc)
    assert out == "x ![missing](table:gone) y"


def test_no_anchor_is_identity():
    doc = Y.Doc()
    text = doc.get("content", type=Y.Text)
    text += "plain text only"
    assert expand_tables(doc) == "plain text only"
