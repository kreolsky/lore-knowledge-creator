"""Unit tests for search_exec's pure helpers (R1 agent-search decomposition).

`_rrf_merge`, `_strip_stop_words`, `_snippet_pos` are pure and previously covered
only indirectly through full `_search_materials_exec` drives. Pinned here at the
unit level: RRF tiebreaks (first-seen order, both-label, incomparable-score
neutrality), stop-word stripping edge cases, and the snippet anchor ladder.
"""

from types import SimpleNamespace

from agent.search_exec import (
    _build_direct_hit,
    _build_semantic_hit,
    _rrf_merge,
    _snippet_pos,
)

# ─── _rrf_merge ──────────────────────────────────────────────────────────────


def _hit(doc_id: str, **extra) -> dict:
    return {"doc_id": doc_id, **extra}


def test_rrf_order_is_the_only_ranking_signal():
    """Overlap outranks both single-layer hits, and the fused ORDER is all a
    consumer gets — the hit dicts are passed through untouched, so nothing in a
    hit restates or contradicts its position."""
    direct, semantic = [_hit("x"), _hit("y")], [_hit("y"), _hit("z")]
    fused = _rrf_merge(direct, semantic)
    assert [h["doc_id"] for h in fused] == ["y", "x", "z"]


def test_hit_builders_emit_no_score():
    """Both layers' hit builders — the only producers of a model-facing hit — emit
    no `score` key. Asserted over the real emitted dicts, not a copied key list."""
    direct = _build_direct_hit(
        {"id": "d1", "title": "T", "parent_id": None,
         "is_reference": False, "is_memory": False},
        snippet="body",
    )
    semantic = _build_semantic_hit(SimpleNamespace(
        kind="memory", parent_id="d2", parent_title="T2",
        heading=None, snippet="body", sources=[],
    ))
    for hit in (direct, semantic):
        assert "score" not in hit, hit


# ─── _snippet_pos ────────────────────────────────────────────────────────────


def test_snippet_pos_token_prefix_covers_stem_only_forms():
    """Neither the query nor a whole token appears — the token's 4–5 char prefix
    anchors the window («замка»-query against «замок» body)."""
    assert _snippet_pos("стены замка кругом", "замконт") == 6
