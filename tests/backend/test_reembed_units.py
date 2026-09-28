"""Unit tests for embeddings' decomposed re-embed helpers (R2 retrieval round).

`_reembed` was a 187-line monolith; its extracted steps are pinned here at the
unit level: the soft-delete-guarded parent fetch, the status UPDATE dedup, the
reuse/fresh store plan (partial-success policy), the CAS transaction builder,
and the finalizing version bump + status/log branch.
"""

from unittest.mock import AsyncMock, patch

import pytest
from markdown_chunker import Chunk


def _chunk(text: str, *, heading: str | None = None) -> Chunk:
    return Chunk(ord=0, heading=heading, content=text,
                 offset_start=0, offset_end=len(text))


# ─── _fetch_reembed_parent ───────────────────────────────────────────────────


class TestFetchReembedParent:
    async def test_returns_the_row_when_live(self):
        from embeddings import _fetch_reembed_parent
        db = AsyncMock()
        db.query = AsyncMock(return_value=[{"content": "b", "content_version": 2}])
        parent = await _fetch_reembed_parent(db, "d1")
        assert parent == {"content": "b", "content_version": 2}
        sql = db.query.call_args.args[0]
        assert "deleted_at IS NONE" in sql
        # The kind rule needs the reference flag on the parent row; the legacy
        # `mem` select (never read by the caller) is dropped alongside.
        assert "is_reference" in sql
        assert "is_memory" in sql
        assert " mem," not in sql

    async def test_returns_none_for_missing_or_soft_deleted(self):
        from embeddings import _fetch_reembed_parent
        db = AsyncMock()
        db.query = AsyncMock(return_value=[])
        assert await _fetch_reembed_parent(db, "d1") is None


# ─── _chunk_kind ─────────────────────────────────────────────────────────────


class TestChunkKind:
    """The corpus label stamped on every chunk — the SAME rule the direct-hit
    layer applies (agent/search_exec.py): memory first, then reference, else
    document."""

    def test_memory_wins_even_over_a_reference_flag(self):
        from embeddings import _chunk_kind
        assert _chunk_kind({"is_memory": True, "is_reference": False}) == "memory"
        assert _chunk_kind({"is_memory": True, "is_reference": True}) == "memory"

    def test_reference_then_document(self):
        from embeddings import _chunk_kind
        assert _chunk_kind({"is_memory": False, "is_reference": True}) == "reference"
        assert _chunk_kind({"is_memory": False, "is_reference": False}) == "document"

    def test_absent_flags_default_to_document(self):
        from embeddings import _chunk_kind
        assert _chunk_kind({}) == "document"


# ─── _mark_embed_ok ──────────────────────────────────────────────────────────


class TestMarkEmbedOk:
    async def test_clears_last_error_by_default(self):
        from embeddings import _mark_embed_ok
        db = AsyncMock()
        await _mark_embed_ok(db, "d1")
        sql, params = db.query.call_args.args
        assert "embedding_status = 'ok'" in sql
        assert "last_embed_error = NONE" in sql

    async def test_partial_success_records_the_dropped_count(self):
        from embeddings import _mark_embed_ok
        db = AsyncMock()
        await _mark_embed_ok(db, "d1", last_error="partial: 2 new chunks rejected")
        sql, params = db.query.call_args.args
        assert "last_embed_error = $err" in sql
        assert params["err"] == "partial: 2 new chunks rejected"


# ─── _embed_and_plan_store ───────────────────────────────────────────────────


class TestEmbedAndPlanStore:
    async def test_all_changed_chunks_embed(self):
        from embeddings import _embed_and_plan_store
        chunks = [_chunk("a"), _chunk("b")]
        hashes = ["h0", "h1"]
        with patch("embeddings._embed_texts_resilient",
                   new_callable=AsyncMock, return_value=([[0.1], [0.2]], [])):
            store, failed = await _embed_and_plan_store(
                "doc", "d1", "T", chunks, hashes, reuse={}, to_embed_idx=[0, 1],
            )
        assert failed == []
        assert [s[3] for s in store] == [None, None]      # reuse_row_id
        assert [s[2] for s in store] == [[0.1], [0.2]]    # fresh embeddings

    async def test_rejected_chunk_dropped_reused_kept(self):
        from embeddings import _embed_and_plan_store
        chunks = [_chunk("a"), _chunk("b")]
        hashes = ["h0", "h1"]
        with patch("embeddings._embed_texts_resilient",
                   new_callable=AsyncMock, return_value=([None], [0])):
            store, failed = await _embed_and_plan_store(
                "doc", "d1", "T", chunks, hashes, reuse={1: "row-b"}, to_embed_idx=[0],
            )
        assert failed == [0]
        assert len(store) == 1
        assert store[0][3] == "row-b" and store[0][2] is None

    async def test_nothing_embedded_and_nothing_reusable_is_an_error(self):
        from embeddings import _embed_and_plan_store
        with patch("embeddings._embed_texts_resilient",
                   new_callable=AsyncMock, return_value=([None], [0])):
            with pytest.raises(RuntimeError, match="rejected all"):
                await _embed_and_plan_store(
                    "doc", "d1", "T", [_chunk("a")], ["h0"], reuse={}, to_embed_idx=[0],
                )

    async def test_pure_reuse_skips_the_provider_call(self):
        from embeddings import _embed_and_plan_store
        resilient = AsyncMock()
        with patch("embeddings._embed_texts_resilient", resilient):
            store, failed = await _embed_and_plan_store(
                "doc", "d1", "T", [_chunk("a")], ["h0"], reuse={0: "row-a"}, to_embed_idx=[],
            )
        resilient.assert_not_awaited()
        assert failed == [] and store[0][3] == "row-a"


# ─── _unchanged ──────────────────────────────────────────────────────────────


class TestUnchanged:
    """The no-op gate — including its model leg, without which the stale-model
    sweep livelocks on a hash-matching pre-column row."""

    def _rows(self, model):
        return [{"content_hash": "h0", "ord": 0, "offset_start": 0,
                 "offset_end": 1, "model": model}]

    def test_matching_signature_and_model_is_unchanged(self):
        from embeddings import _unchanged
        assert _unchanged([_chunk("a")], ["h0"], self._rows("m1"), "m1") is True

    def test_unknown_provenance_is_never_unchanged(self):
        """A pre-column row (model IS NONE) whose hash matches still owes a
        model stamp: skipping the write would re-enqueue it forever (the sweep
        re-runs, the hashes match again, nothing is written)."""
        from embeddings import _unchanged
        assert _unchanged([_chunk("a")], ["h0"], self._rows(None), "m1") is False

    def test_stale_model_is_not_unchanged(self):
        from embeddings import _unchanged
        assert _unchanged([_chunk("a")], ["h0"], self._rows("old"), "m1") is False

    def test_hash_change_is_not_unchanged(self):
        from embeddings import _unchanged
        assert _unchanged([_chunk("a")], ["h-other"], self._rows("m1"), "m1") is False


# ─── _build_reembed_txn ──────────────────────────────────────────────────────


class TestBuildReembedTxn:
    def _store(self):
        reused = (_chunk("a"), "h0", None, "row-a")
        fresh = (_chunk("b"), "h1", [0.1, 0.2], None)
        return [reused, fresh]

    def _txn(self, store=None):
        from embeddings import _build_reembed_txn
        return _build_reembed_txn(
            "d1", "p1", 3, store if store is not None else self._store(),
            kind="document", model="test-model",
        )

    def test_cas_guard_heads_the_statement_list(self):
        stmts, _params = self._txn()
        assert stmts[0].startswith("LET $cur")
        assert "THROW 'version_changed'" in stmts[1]

    def test_delete_keeps_only_stored_rows(self):
        stmts, params = self._txn()
        delete = next(s for s in stmts if s.startswith("DELETE"))
        assert "NOT IN $keep_ids" in delete
        assert params["keep_ids"] == ["row-a"]

    def test_reused_rows_update_in_place_new_rows_carry_the_vector(self):
        stmts, params = self._txn()
        updates = [s for s in stmts if s.startswith("UPDATE type::record('doc_chunks'")]
        creates = [s for s in stmts if s.startswith("CREATE type::record('doc_chunks'")]
        assert len(updates) == 1 and len(creates) == 1
        assert "embedding = " not in updates[0]      # reused keeps its stored vector
        assert "embedding = $emb" in creates[0]
        assert params["rid0"] == "row-a"
        assert params["emb1"] == [0.1, 0.2]
        assert params["ord1"] == 1                   # ord renumbered densely
        assert params["ver"] == 3

    def test_both_arms_stamp_kind_and_model(self):
        """A chunk row carries its corpus label and the model that produced (or,
        for a reused row, still owns) its vector — written on EVERY chunk write,
        reuse arm included, so per-kind retrieval never sees an unlabelled row."""
        stmts, params = self._txn()
        updates = [s for s in stmts if s.startswith("UPDATE type::record('doc_chunks'")]
        creates = [s for s in stmts if s.startswith("CREATE type::record('doc_chunks'")]
        assert len(updates) == 1 and len(creates) == 1
        for arm in (updates[0], creates[0]):
            assert "kind = $kind" in arm
            assert "model = $model" in arm
        assert params["kind"] == "document"
        assert params["model"] == "test-model"

    def test_empty_store_deletes_every_chunk_row(self):
        stmts, params = self._txn([])
        delete = next(s for s in stmts if s.startswith("DELETE"))
        assert "NOT IN" not in delete
        assert "keep_ids" not in params


# ─── _finalize_reembed ───────────────────────────────────────────────────────


class TestFinalizeReembed:
    async def test_bumps_version_then_marks_ok(self):
        from embeddings import _finalize_reembed
        db = AsyncMock()
        db.query = AsyncMock(return_value=None)
        store = [(_chunk("a"), "h0", [0.1], None)]
        await _finalize_reembed(db, "doc", "d1", 3, store=store, failed_idx=[])
        sqls = [c.args[0] for c in db.query.call_args_list]
        assert "content_version = $cv" in sqls[0]
        assert "embedding_status = 'ok'" in sqls[1]
        assert db.query.call_args_list[1].args[1]["id"] == "d1"

    async def test_partial_success_records_dropped_count(self):
        from embeddings import _finalize_reembed
        db = AsyncMock()
        db.query = AsyncMock(return_value=None)
        store = [(_chunk("a"), "h0", None, "row-a")]
        await _finalize_reembed(db, "doc", "d1", 3, store=store, failed_idx=[1])
        last = db.query.call_args_list[-1]
        assert "last_embed_error = $err" in last.args[0]
        assert "partial: 1" in last.args[1]["err"]
