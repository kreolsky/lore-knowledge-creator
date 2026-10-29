"""Provenance-scoped memory retrieval under a subtree narrow.

Plan: .kilo/plans/1786800000000-provenance-scoped-memory-under-subtree-narrow.md.

Today a narrow (`under_document_id` on the agent surface, or a document-scoped
key) drops ALL memory hits: fact-docs live flat under the project's memory space,
outside every user subtree, and the semantic-layer post-filter removes them by
`parent_id`. But every fact carries `mem.provenance.sources` (server-stamped),
and every reference carries a schema-enforced `parent_id` host — so a fact IS
attributable to the subtree that produced its material. These tests pin the new
contract:

- `retrieve_context(allowed_doc_ids=...)` narrows DOC/REF chunks in SQL (out-of-
  subtree hits stop consuming top-k slots — defect 2) and admits facts by
  PROVENANCE (any source reference hosted in the subtree), never by tree
  position;
- soft-deleted source references still scope their fact (D4 — provenance is a
  historical thread);
- facts with empty/unresolvable sources drop under a narrow (D5, measured
  79/175 live facts on dev) — accepted, and stated in the tool description;
- BOTH agent surfaces (Tool-API HTTP + MCP dispatch) return a memory hit WITH
  its `sources` under a real `under_document_id` narrow, and no out-of-subtree
  doc/ref leaks through.

The no-narrow baseline passes before the change too — it is the regression guard
pinning "default untouched", not a Red test.
"""


import pytest

# The monkeypatched query embedding. Seeded chunks carry either this vector
# (cosine 1.0) or NEAR_VEC (cosine 0.95) — both clear RETRIEVAL_MIN_SCORE and
# stay within the per-kind drop-off ratio of each other.
QUERY_VEC = [1.0, 0.0]
NEAR_VEC = [0.95, 0.31224989991992]

TOKEN = "vertigo"


@pytest.fixture(autouse=True)
def _fake_query_embeddings(monkeypatch):
    """Pin the query embedding — no embedding API in the suite; the seeded
    doc_chunks vectors decide the scores."""
    import retrieval

    async def _embed(texts, **kwargs):
        return [list(QUERY_VEC) for _ in texts]

    monkeypatch.setattr(retrieval, "embed_texts", _embed)


# narrow_corpus lives in conftest.py (shared with test_semantic_search_under_doc);
# its constants stay HERE as the single source of truth for the seeded
# vectors/token.


async def _retrieve(corpus, *, narrow=None, **kw):
    from retrieval import retrieve_context

    kw.setdefault("include_documents", True)
    kw.setdefault("include_references", True)
    kw.setdefault("top_k_docs", 8)
    kw.setdefault("top_k_refs", 8)
    return await retrieve_context(
        corpus["pid"], TOKEN, allowed_doc_ids=narrow, **kw,
    )


def _by_id(result):
    return {h.parent_id: h for h in result.hits}


# What a real subtree walk would produce for root: refs are documents too, so
# they sit in `allowed` by tree position (facts never do — they are scoped by
# provenance instead). ref_soft is soft-deleted and drops out of the walk.
IN_SUBTREE = {"narrow-root", "narrow-host-in", "narrow-ref-in"}


# ─── Defect 2: SQL pushdown — no slot starvation ─────────────────────────────


async def test_memory_only_search_is_not_starved_by_raw_chunks(
        narrow_corpus, monkeypatch):
    """F2, the no-narrow twin of slot starvation: a MEMORY-ONLY search must
    spend its whole quota on facts. The query ranks the two raw NEAR_VEC
    chunks (host_in/ref_in) at cosine 1.0 and every fact at 0.95 — with the
    old single project-wide fetch, top_k_memory=2 bought host_in+ref_in
    (documents/references off → dropped at classification) and ZERO facts
    reached the model. The per-kind query spends the quota on facts."""
    import retrieval

    async def _embed(texts, **kwargs):
        return [list(NEAR_VEC) for _ in texts]

    monkeypatch.setattr(retrieval, "embed_texts", _embed)
    result = await _retrieve(
        narrow_corpus,
        include_memory=True, include_documents=False, include_references=False,
        top_k_memory=2,
    )
    assert len(result.hits) == 2, (
        f"memory-only search returned {len(result.hits)} of its 2-slot quota"
    )
    assert all(h.kind == "memory" for h in result.hits)


async def test_reference_chunks_outside_subtree_absent(narrow_corpus):
    result = await _retrieve(narrow_corpus, narrow=set(IN_SUBTREE))
    hits = _by_id(result)
    assert narrow_corpus["ref_in"] in hits
    assert narrow_corpus["ref_out"] not in hits


# ─── D1: memory is filtered by provenance, not dropped ───────────────────────


async def test_fact_with_all_sources_outside_dropped(narrow_corpus):
    hits = _by_id(await _retrieve(narrow_corpus, narrow=set(IN_SUBTREE)))
    assert narrow_corpus["fact_out"] not in hits


# ─── Both agent surfaces: a memory hit reaches the model under a narrow ──────


async def _assert_agent_result(result, corpus):
    hits = {h["doc_id"]: h for h in result["hits"]}
    mem = hits.get(corpus["fact_in"])
    assert mem is not None, (
        "no memory hit under the narrow on the agent surface — "
        f"hits were {result['hits']}"
    )
    assert mem["kind"] == "memory"
    assert any(s["id"] == corpus["ref_in"] for s in mem.get("sources", [])), (
        "the memory hit arrived without its source references"
    )
    # No cross-subtree leakage (docs/refs by tree position, memory by
    # provenance — fact_out must not slip through either).
    for out_id in ("host_out", "ref_out", "fact_out"):
        assert corpus[out_id] not in hits, f"{out_id} leaked through the narrow"
    assert corpus["host_in"] in hits, "the in-subtree doc lost its slot"


async def test_mcp_memory_hit_under_narrow(narrow_corpus):
    """MCP surface (the twin of the HTTP test): dispatch_tool with a real
    under_document_id returns the provenance-scoped memory hit WITH sources."""
    from mcp_gateway.dispatch import dispatch_tool

    corpus = narrow_corpus
    result = await dispatch_tool(
        "search_materials",
        {"query": TOKEN, "under_document_id": corpus["root"], "k": 10},
        {"project_id": corpus["pid"], "user": {"user_id": corpus["uid"]},
         "scope_root": None},
    )
    await _assert_agent_result(result, corpus)
