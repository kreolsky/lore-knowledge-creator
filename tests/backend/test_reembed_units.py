"""Unit tests for embeddings' decomposed re-embed helpers (R2 retrieval round).

`_reembed` was a 187-line monolith; its extracted steps are pinned here at the
unit level: the soft-delete-guarded parent fetch, the status UPDATE dedup, the
reuse/fresh store plan (partial-success policy), the CAS transaction builder,
and the finalizing version bump + status/log branch.
"""

from unittest.mock import AsyncMock, patch

from markdown_chunker import Chunk


def _chunk(text: str, *, heading: str | None = None) -> Chunk:
    return Chunk(ord=0, heading=heading, content=text,
                 offset_start=0, offset_end=len(text))


# ─── _embed_and_plan_store ───────────────────────────────────────────────────


class TestEmbedAndPlanStore:


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


    def test_unknown_provenance_is_never_unchanged(self):
        """A pre-column row (model IS NONE) whose hash matches still owes a
        model stamp: skipping the write would re-enqueue it forever (the sweep
        re-runs, the hashes match again, nothing is written)."""
        from embeddings import _unchanged
        assert _unchanged([_chunk("a")], ["h0"], self._rows(None), "m1") is False


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


# ─── _finalize_reembed ───────────────────────────────────────────────────────


class TestFinalizeReembed:

    async def test_partial_success_records_dropped_count(self):
        from embeddings import _finalize_reembed
        db = AsyncMock()
        db.query = AsyncMock(return_value=None)
        store = [(_chunk("a"), "h0", None, "row-a")]
        await _finalize_reembed(db, "doc", "d1", 3, store=store, failed_idx=[1])
        last = db.query.call_args_list[-1]
        assert "last_embed_error = $err" in last.args[0]
        assert "partial: 1" in last.args[1]["err"]
