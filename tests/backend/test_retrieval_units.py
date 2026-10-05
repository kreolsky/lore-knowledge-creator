"""Unit tests for retrieval's decomposed helpers (R2 retrieval round).

`retrieve_context` was a 233-line monolith; its per-tier helpers are pinned here
at the unit level: the scope pushdown (narrow vs whole project), chunk-hit
classification (kind gating, one-hit-per-fact, anti-monopoly), level-ordered
assembly, and per-kind token budgeting. `_allowed_fact_ids` also gets its first
mocked-DB unit path (previously E2E-only via the subtree-narrow suite).
"""

from unittest.mock import AsyncMock, patch

import config
from retrieval import RetrievalHit, _allowed_fact_ids


def _row(did: str, score: float, content: str = "text", **extra) -> dict:
    return {
        "id": f"c-{did}-{score}", "document_id": did, "heading": None,
        "content": content, "offset_start": 0, "offset_end": len(content),
        "score": score, **extra,
    }


def _meta(title: str = "T", *, is_reference: bool = False, is_memory: bool = False) -> dict:
    return {"title": title, "is_reference": is_reference, "is_memory": is_memory}


# ─── _allowed_fact_ids (unit path; join semantics over a mocked DB) ──────────


class TestAllowedFactIds:
    async def test_fact_joined_by_provenance_to_hosted_ref(self):
        db = AsyncMock()
        db.query = AsyncMock(side_effect=[
            ["ref1", "ref2"],
            [
                {"id": "f1", "mem": {"provenance": {"sources": [
                    {"kind": "reference", "id": "ref1"},
                ]}}},
                {"id": "f2", "mem": {"provenance": {"sources": [
                    {"kind": "reference", "id": "refElsewhere"},
                ]}}},
                {"id": "f3", "mem": None},
            ],
        ])
        out = await _allowed_fact_ids(db, "p1", {"d1"})
        assert out == {"f1"}

    async def test_non_reference_kind_source_never_hosts_a_fact(self):
        db = AsyncMock()
        db.query = AsyncMock(side_effect=[
            ["ref1"],
            [{"id": "f1", "mem": {"provenance": {"sources": [
                {"kind": "document", "id": "ref1"},
            ]}}},
            ],
        ])
        assert await _allowed_fact_ids(db, "p1", {"d1"}) == set()

    async def test_no_hosted_refs_short_circuits_before_the_fact_scan(self):
        db = AsyncMock()
        db.query = AsyncMock(return_value=[])
        assert await _allowed_fact_ids(db, "p1", {"d1"}) == set()
        assert db.query.call_count == 1

    async def test_refs_scan_has_no_deleted_at_filter_fact_scan_keeps_it(self):
        """The two halves of the provenance join disagree ON PURPOSE: soft-deleted
        references still scope a fact (provenance is a historical thread), while a
        soft-deleted FACT is out of every memory surface (R0a's one-axis rule)."""
        db = AsyncMock()
        db.query = AsyncMock(side_effect=[["ref1"], []])
        await _allowed_fact_ids(db, "p1", {"d1"})
        refs_sql = db.query.call_args_list[0].args[0]
        facts_sql = db.query.call_args_list[1].args[0]
        assert "deleted_at" not in refs_sql
        assert "deleted_at IS NONE" in facts_sql


# ─── _fetch_relevant_chunks (per-kind queries, scope pushdown) ───────────────


class TestFetchRelevantChunks:
    """One query per SWITCHED-ON kind — the F2 fix. The old single project-wide
    fetch spent one summed LIMIT on whichever kinds happened to rank highest,
    so an excluded majority starved an included minority (memory-only search on
    a document-heavy project returned a handful of facts)."""

    def _fetch(self, db, **kw):
        from retrieval import _fetch_relevant_chunks
        kw.setdefault("include_documents", True)
        kw.setdefault("include_references", True)
        kw.setdefault("include_memory", True)
        kw.setdefault("top_k_docs", 5)
        kw.setdefault("top_k_refs", 4)
        kw.setdefault("top_k_mem", 3)
        kw.setdefault("allowed_doc_ids", None)
        return _fetch_relevant_chunks(db, "p1", [0.1, 0.2], **kw)

    async def test_one_query_per_switched_on_kind(self):
        db = AsyncMock()
        db.query = AsyncMock(side_effect=[[], [], []])
        await self._fetch(db)
        assert db.query.call_count == 3
        kinds = [c.args[1]["kind"] for c in db.query.call_args_list]
        assert kinds == ["document", "reference", "memory"]
        for call in db.query.call_args_list:
            assert "kind = $kind" in call.args[0]

    async def test_memory_only_runs_the_single_memory_query(self):
        db = AsyncMock()
        db.query = AsyncMock(return_value=[_row("f1", 0.9)])
        rows = await self._fetch(db, include_documents=False,
                                 include_references=False)
        assert [r["document_id"] for r in rows] == ["f1"]
        assert db.query.call_count == 1
        sql, params = db.query.call_args.args
        assert params["kind"] == "memory"

    async def test_limit_is_per_kind_not_summed(self):
        db = AsyncMock()
        db.query = AsyncMock(side_effect=[[], [], []])
        await self._fetch(db)
        quotas = [(c.args[1]["kind"], c.args[1]["top_k"])
                  for c in db.query.call_args_list]
        assert quotas == [("document", 5), ("reference", 4), ("memory", 3)]

    async def test_dim_is_the_query_vector_length(self):
        db = AsyncMock()
        db.query = AsyncMock(side_effect=[[], [], []])
        await self._fetch(db, include_documents=False, include_references=False,
                          top_k_mem=2)
        _sql, params = db.query.call_args.args
        assert params["dim"] == 2
        assert "array::len(embedding) = $dim" in db.query.call_args.args[0]

    async def test_stale_model_predicate_excludes_known_stale_only(self):
        """(model = $model OR model IS NONE): NONE is 'unknown, pre-column'
        provenance and stays searchable through the post-swap drain; a row
        stamped with ANOTHER model is KNOWN-stale and never fetched."""
        db = AsyncMock()
        db.query = AsyncMock(side_effect=[[], [], []])
        await self._fetch(db)
        sql, params = db.query.call_args.args
        assert "(model = $model OR model IS NONE)" in sql
        assert params["model"], "the current model must ride the params"

    async def test_no_narrow_no_scope_clause_on_any_kind_query(self):
        db = AsyncMock()
        db.query = AsyncMock(side_effect=[[], [], []])
        await self._fetch(db)
        for call in db.query.call_args_list:
            assert "allowed_docs" not in call.args[0]
            assert "allowed_facts" not in call.args[0]

    async def test_narrow_scope_clause_per_kind(self):
        """Doc/REF chunks scope by tree position ($allowed_docs — a reference
        IS a documents row); MEMORY scopes by provenance ($allowed_facts). The
        old OR-of-both clause made every kind's query carry both lists."""
        db = AsyncMock()
        db.query = AsyncMock(side_effect=[
            ["ref1"],                                   # _allowed_fact_ids: refs
            [{"id": "f1", "mem": {"provenance": {"sources": [
                {"kind": "reference", "id": "ref1"},
            ]}}}],                                      # _allowed_fact_ids: facts
            [_row("d1", 0.9)],                          # document rows
            [_row("r1", 0.8)],                          # reference rows
            [_row("f1", 0.7)],                          # memory rows
        ])
        rows = await self._fetch(db, allowed_doc_ids={"d1"})
        assert [r["document_id"] for r in rows] == ["d1", "r1", "f1"]
        doc_sql, ref_sql, mem_sql = (
            c.args[0] for c in db.query.call_args_list[2:]
        )
        for sql in (doc_sql, ref_sql):
            assert "document_id IN $allowed_docs" in sql
            assert "allowed_facts" not in sql
        assert "document_id IN $allowed_facts" in mem_sql
        assert "allowed_docs" not in mem_sql
        mem_params = db.query.call_args_list[-1].args[1]
        assert mem_params["allowed_facts"] == ["f1"]
        assert db.query.call_args_list[2].args[1]["allowed_docs"] == ["d1"]

    async def test_narrow_with_memory_off_skips_the_fact_scan(self):
        db = AsyncMock()
        db.query = AsyncMock(return_value=[_row("d1", 0.9)])
        await self._fetch(db, include_memory=False, allowed_doc_ids={"d1"})
        assert db.query.call_count == 2  # doc + ref queries; no fact scan, no memory query
        for call in db.query.call_args_list:
            assert call.args[1]["allowed_facts"] == []


# ─── _fetch_relevant_chunks (model/dimension predicate over REAL rows) ───────


class TestFetchModelPredicateRealRows:
    """The model + dimension legs against a real DB: a KNOWN-stale row never
    surfaces, a NONE row survives the drain window, and a wrong-dimension row
    is OMITTED where cosine alone would have raised (a crash into an omission
    is the whole point of the guard)."""

    async def test_stale_excluded_none_and_current_included(self,
                                                            project_with_doc,
                                                            monkeypatch):
        import settings as settings_mod

        import retrieval
        from db import create_record, get_db

        pid, _host_id, _uid = project_with_doc
        current = await settings_mod.get("EMBEDDING_MODEL")

        doc_id, wide_id = "retmodel-doc", "retmodel-wide"
        db = await get_db()
        for did in (doc_id, wide_id):
            await db.query("DELETE type::record('documents', $id)", {"id": did})
            await db.query("DELETE doc_chunks WHERE document_id = $id", {"id": did})
            await create_record("documents", did, {
                "project_id": pid, "parent_id": None, "title": did,
                "content": f"body of {did}", "path": f"{did}.md",
                "is_reference": False,
            })

        async def chunk(cid, did, content, model, vec):
            await db.query(
                "CREATE type::record('doc_chunks', $cid) SET document_id = $did, "
                "project_id = $pid, ord = 0, heading = NONE, content = $c, "
                "offset_start = 0, offset_end = 4, content_version = 0, "
                "kind = 'document', model = $model, embedding = $emb",
                {"cid": cid, "did": did, "pid": pid, "c": content,
                 "model": model, "emb": vec},
            )

        await chunk("retmodel-c-none", doc_id, "row-none", None, [1.0, 0.0])
        await chunk("retmodel-c-stale", doc_id, "row-stale", "other-model", [1.0, 0.0])
        await chunk("retmodel-c-current", doc_id, "row-current", current, [1.0, 0.0])
        # A future swap's wrong-dimension row: cosine against the 2-dim query
        # would raise; the guard must omit it silently instead.
        await chunk("retmodel-c-wide", wide_id, "row-wide", None,
                    [1.0, 0.0, 0.0, 0.0])

        async def _embed(texts, **kwargs):
            return [[1.0, 0.0] for _ in texts]

        monkeypatch.setattr(retrieval, "embed_texts", _embed)
        result = await retrieval.retrieve_context(
            pid, "row", include_documents=True, include_references=False,
            include_memory=False, top_k_docs=10, top_k_refs=10,
        )
        snippets = [h.snippet for h in result.hits]
        try:
            assert "row-none" in snippets, "a NONE-model row vanished from search"
            assert "row-current" in snippets, "a current-model row vanished from search"
            assert "row-stale" not in snippets, "a KNOWN-stale row surfaced"
            assert "row-wide" not in snippets, "a wrong-dimension row surfaced"
        finally:
            for did in (doc_id, wide_id):
                await db.query("DELETE type::record('documents', $id)", {"id": did})
                await db.query("DELETE doc_chunks WHERE document_id = $id", {"id": did})


# ─── _classify_chunk_hits (kind gating, dedupe, anti-monopoly) ───────────────


class TestClassifyChunkHits:
    def _classify(self, rows, meta, **kw):
        from retrieval import _classify_chunk_hits
        # min_score and max_per_doc resolve HERE (config read at call time, so
        # the patch contexts below shape them) — the production caller resolves it through
        # settings.get instead.
        include = {"include_documents": True, "include_references": True,
                   "include_memory": True, "min_score": config.RETRIEVAL_MIN_SCORE,
                   "max_per_doc": config.RETRIEVAL_MAX_PER_DOC}
        include.update(kw)
        return _classify_chunk_hits(rows, meta, **include)

    def test_kind_split_by_parent_meta(self):
        meta = {
            "d1": _meta("Doc"),
            "r1": _meta("Ref", is_reference=True),
            "f1": _meta("Fact", is_memory=True),
        }
        doc, ref, mem = self._classify(
            [_row("d1", 0.9), _row("r1", 0.8), _row("f1", 0.7)], meta,
        )
        assert [h.parent_id for h in doc] == ["d1"]
        assert [h.parent_id for h in ref] == ["r1"]
        assert [h.parent_id for h in mem] == ["f1"]
        assert doc[0].kind == "document" and ref[0].kind == "reference"
        assert mem[0].kind == "memory"

    def test_one_hit_per_fact_even_when_several_chunks_match(self):
        meta = {"f1": _meta("Fact", is_memory=True)}
        doc, ref, mem = self._classify([_row("f1", 0.9), _row("f1", 0.8)], meta)
        assert len(mem) == 1
        assert mem[0].score == 0.9  # the strongest matching chunk represents the fact

    def test_include_flags_gate_each_kind(self):
        meta = {
            "d1": _meta("Doc"),
            "r1": _meta("Ref", is_reference=True),
            "f1": _meta("Fact", is_memory=True),
        }
        rows = [_row("d1", 0.9), _row("r1", 0.8), _row("f1", 0.7)]
        doc, ref, mem = self._classify(rows, meta, include_memory=False)
        assert not mem and doc and ref
        doc, ref, mem = self._classify(rows, meta, include_references=False)
        assert not ref and doc and mem
        doc, ref, mem = self._classify(rows, meta, include_documents=False)
        assert not doc and ref and mem

    def test_unknown_parent_and_subfloor_rows_dropped(self):
        with patch.object(config, "RETRIEVAL_MIN_SCORE", 0.5):
            doc, ref, mem = self._classify(
                [_row("ghost", 0.9), _row("d1", 0.1)], {"d1": _meta("Doc")},
            )
        assert not doc and not ref and not mem

    def test_anti_monopoly_caps_chunks_per_document(self):
        meta = {"d1": _meta("Doc")}
        rows = [_row("d1", 0.9 - i * 0.01) for i in range(5)]
        with patch.object(config, "RETRIEVAL_MAX_PER_DOC", 2):
            doc, ref, mem = self._classify(rows, meta)
        assert len(doc) == 2
        assert [h.score for h in doc] == [0.9, 0.89]  # keeps the strongest

    def test_doc_hit_keeps_position_memory_and_ref_hits_do_not(self):
        meta = {"d1": _meta("Doc"), "f1": _meta("Fact", is_memory=True)}
        row = _row("d1", 0.9, heading="H")
        row["offset_start"], row["offset_end"] = 3, 9
        doc, ref, mem = self._classify(
            [row, _row("f1", 0.8)], meta,
        )
        assert doc[0].heading == "H" and doc[0].offset_start == 3
        assert mem[0].heading is None and mem[0].offset_end is None


# ─── _assemble_hits (drop-off, fact payloads, level ordering) ────────────────


class TestAssembleHits:
    async def test_fact_level_precedes_chunk_level_regardless_of_score(self):
        from retrieval import _assemble_hits
        db = AsyncMock()
        db.query = AsyncMock(return_value=[])
        mem = [RetrievalHit(kind="memory", parent_id="f1", parent_title="F",
                            heading=None, snippet="fact", offset_start=None,
                            offset_end=None, score=0.3)]
        doc = [RetrievalHit(kind="document", parent_id="d1", parent_title="D",
                            heading=None, snippet="chunk", offset_start=None,
                            offset_end=None, score=0.95)]
        hits = await _assemble_hits(db, doc, [], mem)
        assert hits[0].parent_id == "f1"

    async def test_fact_payloads_loaded_for_surviving_memory_hits(self):
        from retrieval import _assemble_hits
        db = AsyncMock()
        db.query = AsyncMock(side_effect=[
            [{"id": "f1", "content": "whole fact body", "mem": {"provenance": {
                "sources": [{"kind": "reference", "id": "ref1"}],
            }}}],
            [{"id": "ref1", "title": "The Source"}],
        ])
        mem = [RetrievalHit(kind="memory", parent_id="f1", parent_title="F",
                            heading=None, snippet="chunk", offset_start=None,
                            offset_end=None, score=0.9)]
        hits = await _assemble_hits(db, [], [], mem)
        assert hits[0].snippet == "whole fact body"
        assert hits[0].sources == [{"id": "ref1", "title": "The Source"}]


# ─── _apply_token_budgets ────────────────────────────────────────────────────


class TestApplyTokenBudgets:
    def _hit(self, kind: str, snippet: str) -> RetrievalHit:
        return RetrievalHit(kind=kind, parent_id=snippet, parent_title="",
                            heading=None, snippet=snippet, offset_start=None,
                            offset_end=None, score=0.9)

    def test_budgets_are_per_kind_no_borrowing(self):
        from retrieval import _apply_token_budgets
        hits = [self._hit("document", "x" * 400), self._hit("reference", "y" * 100)]
        out = _apply_token_budgets(hits, budget_docs=10, budget_refs=10_000, budget_mem=10_000)
        assert [h.kind for h in out] == ["reference"]

    def test_memory_never_shares_the_docs_budget(self):
        from retrieval import _apply_token_budgets
        hits = [self._hit("document", "x" * 400), self._hit("memory", "fact")]
        out = _apply_token_budgets(hits, budget_docs=10, budget_refs=10, budget_mem=10_000)
        assert [h.kind for h in out] == ["memory"]

    def test_smaller_later_hit_fits_after_an_oversized_one_is_skipped(self):
        from retrieval import _apply_token_budgets
        hits = [self._hit("document", "x" * 400), self._hit("document", "ok")]
        out = _apply_token_budgets(hits, budget_docs=50, budget_refs=50, budget_mem=50)
        assert [h.snippet for h in out] == ["ok"]
