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
    _strip_stop_words,
)

# ─── _rrf_merge ──────────────────────────────────────────────────────────────


def _hit(doc_id: str, **extra) -> dict:
    return {"doc_id": doc_id, **extra}


def test_rrf_ranks_doc_found_by_both_above_single_layer_hits():
    """A doc found by BOTH layers is the strongest signal: reciprocal sums beat
    any single-layer rank, regardless of which layer ranked it where."""
    direct = [_hit("both", source="direct"), _hit("d-only")]
    semantic = [_hit("s-only"), _hit("both", source="semantic")]
    fused = _rrf_merge(direct, semantic)
    assert fused[0]["doc_id"] == "both"
    assert fused[0]["source"] == "both"


def test_rrf_mirror_orders_by_reciprocal_sums_with_doc_id_tiebreak():
    """Mirror ranks: 1+3 beats 2+2 under reciprocal sums, and the 1+3 tie is
    broken deterministically by doc_id (the code's own tiebreak), not fold luck."""
    direct = [_hit("a"), _hit("b"), _hit("c")]
    semantic = [_hit("c"), _hit("b"), _hit("a")]  # mirror order
    fused = _rrf_merge(direct, semantic)
    assert [h["doc_id"] for h in fused] == ["a", "c", "b"]


def test_rrf_single_layers_keep_rank_order():
    """With no overlap, fusion order is each layer's rank interleaved by
    reciprocal score — the direct list's head wins (rank 1 beats rank 1 of the
    second-folded list only on ties by first-seen; here sums differ)."""
    direct = [_hit("d1"), _hit("d2")]
    semantic = [_hit("s1"), _hit("s2")]
    fused = _rrf_merge(direct, semantic)
    ids = [h["doc_id"] for h in fused]
    assert set(ids) == {"d1", "d2", "s1", "s2"}
    # d1 and s1 both carry rank-1 sums; first-seen (direct) wins the head.
    assert ids[0] == "d1"


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


def test_rrf_empty_inputs():
    assert _rrf_merge([], []) == []
    assert [h["doc_id"] for h in _rrf_merge([_hit("a")], [])] == ["a"]
    assert [h["doc_id"] for h in _rrf_merge([], [_hit("a")])] == ["a"]


# ─── _strip_stop_words ───────────────────────────────────────────────────────


def test_strip_removes_russian_stop_words():
    assert _strip_stop_words("что известно про замок") == "известно замок"


def test_strip_only_stop_words_empties_to_signal_degrade():
    """'' is the degrade signal — the caller falls back to the raw query."""
    assert _strip_stop_words("и в не на") == ""


def test_strip_is_case_insensitive_and_collapses_whitespace():
    assert _strip_stop_words("  Замок   И ЕГО Владельца ") == "замок владельца"


def test_strip_keeps_unknown_function_words():
    """The set is a fan-out guard, not a parser — unknown words survive."""
    assert _strip_stop_words("зетариновый кристалл") == "зетариновый кристалл"


# ─── _snippet_pos ────────────────────────────────────────────────────────────


def test_snippet_pos_whole_query_first():
    assert _snippet_pos("текст про замок и стены", "про замок") == 6


def test_snippet_pos_longest_token_when_query_absent():
    """Stem-only hit: the query string is absent, its longest token is present."""
    assert _snippet_pos("владелец замка ушёл", "владельца замка") == 0


def test_snippet_pos_token_prefix_covers_stem_only_forms():
    """Neither the query nor a whole token appears — the token's 4–5 char prefix
    anchors the window («замка»-query against «замок» body)."""
    assert _snippet_pos("стены замка кругом", "замконт") == 6


def test_snippet_pos_minus_one_when_nothing_matches():
    assert _snippet_pos("совсем другой текст", "отсутствует") == -1


def test_snippet_pos_ignores_short_tokens():
    """Tokens under 3 chars are skipped — they anchor nothing reliably."""
    assert _snippet_pos("аб аб аб", "аб вг") == -1


# ─── the whole-project parent-map scan is out of the hot path (R1) ──────────


async def test_direct_layer_never_runs_the_parent_map_scan():
    """rank_rows is no-anchor by design, and tree_distance consults a parent map
    only when an anchor exists — so the whole-project id+parent_id scan that used
    to precede EVERY search was pure waste. A map-shaped SELECT (id + parent_id,
    no title/content, no matcher) reaching the direct layer is the regression."""
    seen: list[str] = []

    class FakeDB:
        async def query(self, stmt, params=None):
            seen.append(stmt)
            return [
                {"id": "d1", "title": "doc", "content": "body",
                 "is_reference": False, "parent_id": None, "is_memory": False,
                 "updated_at": None, "sort_key": ""},
            ]

    from agent.search_exec import _direct_lexical_layer

    warnings: list[str] = []
    hits = await _direct_lexical_layer(
        FakeDB(), project_id="p1", query="body", qlower="body", exact=True,
        allowed=None, corpus="all", k=5,
        warnings=warnings,
    )
    assert hits and hits[0]["doc_id"] == "d1"
    map_scans = [
        s for s in seen
        if "SELECT meta::id(id) AS id, parent_id FROM documents" in s
    ]
    assert not map_scans, (
        f"the whole-project parent-map scan returned to the search hot path: {map_scans}"
    )
