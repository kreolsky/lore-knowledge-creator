"""Unit tests for retrieval pipeline — mocked DB and embedding API."""

from unittest.mock import AsyncMock, patch

import config
from embeddings import EmbeddingConfigError
from retrieval import (
    _apply_drop_off,
    retrieve_context,
)


class TestRetrieveContext:


    @patch("retrieval.embed_texts", new_callable=AsyncMock, side_effect=EmbeddingConfigError("no config"))
    async def test_retrieve_config_error(self, mock_embed):
        result = await retrieve_context("proj1", "query", top_k_docs=8, top_k_refs=8, include_documents=True, include_references=True)
        assert result.error == "not_configured"

    @patch.object(config, "RETRIEVAL_SCORE_DROP_OFF", 0.7)
    @patch("retrieval.embed_texts", new_callable=AsyncMock)
    @patch("retrieval.get_db", new_callable=AsyncMock)
    async def test_retrieve_per_kind_dropoff(self, mock_get_db, mock_embed):
        # One query PER KIND: the doc scan and the reference scan are separate
        # statements (kind split happens in SQL, pre-LIMIT), then the parent-meta
        # join. Per-kind drop-off still applies within each kind's hits.
        mock_embed.return_value = [[0.5] * 128]
        doc_chunks = [
            {"id": "c1", "document_id": "d1", "heading": None, "content": "doc text",
             "offset_start": 0, "offset_end": 8, "score": 0.85},
        ]
        ref_chunks = [
            {"id": "r1", "document_id": "ref1", "heading": None, "content": "ref text",
             "offset_start": 0, "offset_end": 8, "score": 0.55},
            {"id": "r2", "document_id": "ref2", "heading": None, "content": "ref text 2",
             "offset_start": 0, "offset_end": 10, "score": 0.20},
        ]
        parent_meta = [
            {"id": "d1", "title": "Doc", "is_reference": False},
            {"id": "ref1", "title": "Ref1", "is_reference": True},
            {"id": "ref2", "title": "Ref2", "is_reference": True},
        ]
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[doc_chunks, ref_chunks, parent_meta])
        mock_get_db.return_value = mock_db

        result = await retrieve_context("proj1", "query", top_k_docs=8, top_k_refs=8, include_documents=True,
                                        include_references=True, include_memory=False)
        ref_hits = [h for h in result.hits if h.kind == "reference"]
        assert any(h.parent_id == "ref1" for h in ref_hits)
        assert not any(h.parent_id == "ref2" for h in ref_hits)


class TestApplyDropOff:
    def test_single_hit(self):
        from retrieval import RetrievalHit
        hit = RetrievalHit(kind="document", parent_id="d1", parent_title="", heading=None,
                           snippet="text", offset_start=0, offset_end=4, score=0.9)
        assert _apply_drop_off([hit], config.RETRIEVAL_SCORE_DROP_OFF) == [hit]

    def test_drop_off_cuts(self):
        from retrieval import RetrievalHit
        hits = [
            RetrievalHit(kind="document", parent_id="d1", parent_title="", heading=None,
                         snippet="a", offset_start=0, offset_end=1, score=0.9),
            RetrievalHit(kind="document", parent_id="d2", parent_title="", heading=None,
                         snippet="b", offset_start=0, offset_end=1, score=0.5),
        ]
        with patch.object(config, "RETRIEVAL_SCORE_DROP_OFF", 0.7):
            result = _apply_drop_off(hits, config.RETRIEVAL_SCORE_DROP_OFF)
        assert len(result) == 1
        assert result[0].parent_id == "d1"

    def test_no_drop_off_gradual(self):
        from retrieval import RetrievalHit
        hits = [
            RetrievalHit(kind="document", parent_id=f"d{i}", parent_title="", heading=None,
                         snippet="x", offset_start=0, offset_end=1, score=0.9 - i * 0.05)
            for i in range(5)
        ]
        with patch.object(config, "RETRIEVAL_SCORE_DROP_OFF", 0.5):
            result = _apply_drop_off(hits, config.RETRIEVAL_SCORE_DROP_OFF)
        assert len(result) == 5
