"""doc_proximity unit tests — backend mirror of document-sort.test.ts.

# SYSTEM: doc-proximity-tests — pure-Python ranking; mirrors frontend tier/
# distance/recency/tiebreak cases. Only the pure ranking primitives (no DB).
"""

from datetime import datetime, timezone

from doc_proximity import (
    name_match_tier,
    rank_rows,
    tree_distance,
)


def _row(id_, title, *, parent_id=None, content="", updated_at="2025-01-01T00:00:00Z", sort_key=""):
    return {
        "id": id_, "title": title, "content": content, "is_reference": False,
        "parent_id": parent_id, "updated_at": updated_at, "sort_key": sort_key,
    }


def _map(rows):
    return {r["id"]: r["parent_id"] for r in rows}


# ─── tree_distance ───────────────────────────────────────────────────────────


def test_tree_distance_self_is_zero():
    assert tree_distance("a", "a", {"a": None}) == 0


def test_tree_distance_sibling_vs_cousin():
    m = {"root": None, "anchor": "root", "sibling": "root", "cousin": "sibling"}
    assert tree_distance("anchor", "sibling", m) == 2
    assert tree_distance("anchor", "cousin", m) == 3
    assert tree_distance("anchor", "sibling", m) < tree_distance("anchor", "cousin", m)


def test_tree_distance_cross_subtree_is_finite():
    m = {"a": "t1", "b": "t2", "anchor": "t3"}
    d = tree_distance("anchor", "a", m)
    assert d > 0 and d != float("inf")


def test_tree_distance_parent_child():
    m = {"parent": None, "child": "parent"}
    assert tree_distance("child", "parent", m) == 1


# ─── name_match_tier ─────────────────────────────────────────────────────────


def test_name_match_tiers():
    assert name_match_tier("Alpha", "alpha") == 0
    assert name_match_tier("Alpha Beta", "alpha") == 1
    assert name_match_tier("X-Alpha-Y", "alpha") == 2
    assert name_match_tier("Other", "alpha", content="has alpha word") == 3
    assert name_match_tier("Other", "alpha", content="nothing") == 4


# ─── rank_rows ───────────────────────────────────────────────────────────────


def _ids(ranked):
    return [r["id"] for r in ranked]


def test_rank_exact_before_starts_with():
    rows = [_row("1", "Alpha Beta"), _row("2", "Alpha")]
    assert _ids(rank_rows(rows, anchor_id=None, query="alpha")) == ["2", "1"]


def test_rank_closer_tree_distance_wins_at_same_tier():
    rows = [
        _row("root", "Root", parent_id=None),
        _row("sibling", "Alpha A", parent_id="root"),
        _row("current", "Current", parent_id="root"),
        _row("distant", "Alpha B", parent_id="sibling"),
    ]
    cands = [r for r in rows if r["title"].lower().startswith("alpha")]
    assert _ids(rank_rows(cands, anchor_id="current", query="alpha")) == ["sibling", "distant"]


def test_rank_recency_tiebreak():
    rows = [
        _row("a", "Alpha Beta", updated_at="2025-01-01T00:00:00Z"),
        _row("b", "Alpha Gamma", updated_at="2025-06-01T00:00:00Z"),
    ]
    # same tier (1 for both, starts-with "alpha"), no anchor → recency DESC → b first
    assert _ids(rank_rows(rows, anchor_id=None, query="alpha")) == ["b", "a"]


def test_rank_no_anchor_skips_distance():
    rows = [
        _row("a", "Test A", parent_id="x", updated_at="2025-01-01T00:00:00Z"),
        _row("b", "Test B", parent_id=None, updated_at="2025-06-01T00:00:00Z"),
    ]
    assert _ids(rank_rows(rows, anchor_id=None, query="test")) == ["b", "a"]


def test_rank_self_first():
    rows = [_row("a", "Alpha"), _row("b", "Beta")]
    assert rank_rows(rows, anchor_id="a", query="a")[0]["id"] == "a"


def test_rank_empty_input():
    assert rank_rows([], anchor_id="x", query="q") == []


def test_rank_sort_key_tiebreak():
    rows = [
        _row("b", "Beta", sort_key="a5", updated_at="2025-01-01T00:00:00Z"),
        _row("a", "alpha", sort_key="a1", updated_at="2025-01-01T00:00:00Z"),
    ]
    # same tier + same recency, no anchor → sort_key ASC → a (a1) first
    assert _ids(rank_rows(rows, anchor_id=None, query=""))[0] == "a"


def test_rank_document_id_tiebreak():
    rows = [
        _row("b", "Beta", sort_key="a5"),
        _row("a", "alpha", sort_key="a5"),
    ]
    assert _ids(rank_rows(rows, anchor_id=None, query="")) == ["a", "b"]


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


def test_rank_datetime_object_handled():
    rows = [
        _row("a", "Alpha Beta", updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc)),
        _row("b", "Alpha Gamma", updated_at=datetime(2025, 6, 1, tzinfo=timezone.utc)),
    ]
    assert _ids(rank_rows(rows, anchor_id=None, query="alpha")) == ["b", "a"]
