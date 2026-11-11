"""`expand_tables(doc)` — derived-plaintext expansion of table anchors.

Builds a pycrdt Doc with the `tables` subtree shape and asserts each `![label](table:id)`
anchor in `content` expands to a GFM table, splicing at the anchor offset.
"""
import pycrdt as Y
from helpers import make_table

from table_serialize import expand_tables


def test_expand_replaces_anchor_with_gfm():
    doc = Y.Doc()
    text = doc.get("content", type=Y.Text)
    text += "intro\n\n![Cast](table:t1)\n\nouter"
    make_table(doc, "t1", [["Name", "Role"], ["Red", "Hero"]])

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
