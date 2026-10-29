"""doc_proximity unit tests — backend mirror of document-sort.test.ts.

# SYSTEM: doc-proximity-tests — pure-Python ranking; mirrors frontend tier/
# distance/recency/tiebreak cases. Only the pure ranking primitives (no DB).
"""


from doc_proximity import (
    rank_rows,
    tree_distance,
)


def _row(id_, title, *, parent_id=None, content="", updated_at="2025-01-01T00:00:00Z", sort_key=""):
    return {
        "id": id_, "title": title, "content": content, "is_reference": False,
        "parent_id": parent_id, "updated_at": updated_at, "sort_key": sort_key,
    }


# ─── tree_distance ───────────────────────────────────────────────────────────


def test_tree_distance_cross_subtree_is_finite():
    m = {"a": "t1", "b": "t2", "anchor": "t3"}
    d = tree_distance("anchor", "a", m)
    assert d > 0 and d != float("inf")


# ─── rank_rows ───────────────────────────────────────────────────────────────


def test_rank_deeply_nested_chain():
    rows = [
        _row("l0", "Level 0", parent_id=None),
        _row("l1", "Level 1", parent_id="l0"),
        _row("l2", "Level 2", parent_id="l1"),
        _row("l3", "Target", parent_id="l2"),
    ]
    cands = [r for r in rows if r["title"].startswith("Level") or r["title"] == "Target"]
    ranked = rank_rows(cands, anchor_id="l2", query="")
    assert ranked[0]["id"] == "l2"
