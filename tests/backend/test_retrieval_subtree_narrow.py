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

import hashlib
import secrets

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
    return await retrieve_context(
        corpus["pid"], TOKEN, [], allowed_doc_ids=narrow, **kw,
    )


def _by_id(result):
    return {h.parent_id: h for h in result.hits}


# What a real subtree walk would produce for root: refs are documents too, so
# they sit in `allowed` by tree position (facts never do — they are scoped by
# provenance instead). ref_soft is soft-deleted and drops out of the walk.
IN_SUBTREE = {"narrow-root", "narrow-host-in", "narrow-ref-in"}


# ─── Baseline: no narrow ⇒ unchanged ─────────────────────────────────────────


async def test_no_narrow_keeps_memory_and_whole_project(narrow_corpus):
    """The whole-project default is untouched (the INVARIANT in search_exec):
    without allowed_doc_ids every fact is served, including out-subtree docs —
    memory included. This is the regression guard, not a Red test."""
    hits = _by_id(await _retrieve(narrow_corpus))

    for f in ("fact_in", "fact_out", "fact_merged", "fact_soft", "fact_empty"):
        assert narrow_corpus[f] in hits, f"{f} must be served with no narrow"
    assert narrow_corpus["host_out"] in hits
    assert narrow_corpus["ref_out"] in hits


# ─── Defect 2: SQL pushdown — no slot starvation ─────────────────────────────


async def test_out_of_subtree_chunks_absent_and_no_slot_starvation(narrow_corpus):
    """Under a narrow, out-of-subtree DOC and REF chunks are excluded IN SQL.
    host_out carries the PERFECT vector and top_k_docs=1: with the old
    project-wide fetch it consumed the only slot before host_in was ever seen;
    with the pushdown the in-subtree hit is what the slot buys."""
    result = await _retrieve(
        narrow_corpus,
        narrow=set(IN_SUBTREE),
        include_memory=False, include_references=False, top_k_docs=1,
    )
    hits = _by_id(result)
    assert narrow_corpus["host_in"] in hits, (
        "the in-subtree doc lost its top-k slot to an out-of-subtree hit"
    )
    assert narrow_corpus["host_out"] not in hits


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


async def test_fact_with_source_hosted_inside_returned_with_sources(narrow_corpus):
    """The headline case: a fact-doc OUTSIDE the subtree by tree position is
    returned under the narrow because its source reference is hosted INSIDE —
    and it rides with `sources` (the journal thread the model must judge it
    against)."""
    hits = _by_id(await _retrieve(narrow_corpus, narrow=set(IN_SUBTREE)))

    hit = hits.get(narrow_corpus["fact_in"])
    assert hit is not None, "provenance-scoped fact dropped under the narrow"
    assert hit.kind == "memory"
    assert [s["id"] for s in hit.sources] == [narrow_corpus["ref_in"]]
    assert hit.sources[0]["title"], "a source without a title is unreadable"


async def test_fact_with_all_sources_outside_dropped(narrow_corpus):
    hits = _by_id(await _retrieve(narrow_corpus, narrow=set(IN_SUBTREE)))
    assert narrow_corpus["fact_out"] not in hits


async def test_merged_fact_spanning_in_and_out_subtrees_returned(narrow_corpus):
    """∃-semantics: a fact distilled from two seminars belongs to the narrow if
    ANY of its sources is hosted inside (D1)."""
    hits = _by_id(await _retrieve(narrow_corpus, narrow=set(IN_SUBTREE)))
    assert narrow_corpus["fact_merged"] in hits


async def test_soft_deleted_source_ref_still_scopes_fact(narrow_corpus):
    """D4: provenance is a historical thread — a fact does not stop being about
    a subtree because its source was soft-deleted. Only a hard-missing row is
    unresolvable."""
    hits = _by_id(await _retrieve(narrow_corpus, narrow=set(IN_SUBTREE)))
    assert narrow_corpus["fact_soft"] in hits


async def test_fact_with_empty_sources_dropped_under_narrow(narrow_corpus):
    """D5: legacy facts without provenance drop under a narrow (79/175 on dev).
    Accepted — the shortfall is stated in the tool description, not silently
    swallowed. Without a narrow they are still served (baseline test above)."""
    hits = _by_id(await _retrieve(narrow_corpus, narrow=set(IN_SUBTREE)))
    assert narrow_corpus["fact_empty"] not in hits


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


async def _make_agent_key(test_db, user_id: str, project_id: str) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    await create_record("api_keys", f"narrow-agent-key-{secrets.token_hex(4)}", {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": "",  # project-scoped
        "token_hash": token_hash,
        "label": "agent",
        "capabilities": ["agent"],
    })
    return token


async def test_http_memory_hit_under_narrow(
    client, test_db, narrow_corpus,
):
    """Tool-API surface: POST /api/tool/search_materials with a real
    under_document_id returns the provenance-scoped memory hit WITH sources."""
    corpus = narrow_corpus
    token = await _make_agent_key(test_db, corpus["uid"], corpus["pid"])

    resp = await client.post(
        "/api/tool/search_materials",
        json={"query": TOKEN, "under_document_id": corpus["root"],
              "k": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    await _assert_agent_result(resp.json(), corpus)


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
