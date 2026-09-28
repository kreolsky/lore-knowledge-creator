"""Unit tests for retrieval pipeline — mocked DB and embedding API."""

from unittest.mock import AsyncMock, MagicMock, patch

import config
from embeddings import EmbeddingConfigError
from retrieval import (
    RetrievalResult,
    _apply_drop_off,
    _rewrite_query,
    retrieve_context,
)


class TestRetrieveContext:
    @patch("retrieval.embed_texts", new_callable=AsyncMock)
    @patch("retrieval.get_db", new_callable=AsyncMock)
    async def test_retrieve_empty_project(self, mock_get_db, mock_embed):
        mock_embed.return_value = [[0.1] * 128]
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(return_value=[])
        mock_get_db.return_value = mock_db

        result = await retrieve_context("proj1", "query", [], include_documents=True, include_references=True)
        assert isinstance(result, RetrievalResult)
        assert result.hits == []

    @patch("retrieval.embed_texts", new_callable=AsyncMock)
    @patch("retrieval.get_db", new_callable=AsyncMock)
    async def test_retrieve_doc_hits(self, mock_get_db, mock_embed):
        mock_embed.return_value = [[0.5] * 128]
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[
            [
                {"id": "c1", "document_id": "d1", "heading": "H1", "content": "Chunk text",
                 "offset_start": 0, "offset_end": 10, "score": 0.9},
            ],
            [{"id": "d1", "title": "Doc One", "is_reference": False}],
        ])
        mock_get_db.return_value = mock_db

        # include_memory=False: the fetch is ONE query per switched-on kind, so
        # the canned side_effect maps 1:1 (document scan → parent-meta join).
        # This test exercises doc-hit assembly, not memory.
        result = await retrieve_context("proj1", "query", [], include_documents=True,
                                        include_references=False, include_memory=False)
        assert len(result.hits) == 1
        assert result.hits[0].parent_id == "d1"
        assert result.hits[0].kind == "document"

    @patch("retrieval.RETRIEVAL_MAX_PER_DOC", 2)
    @patch("retrieval.embed_texts", new_callable=AsyncMock)
    @patch("retrieval.get_db", new_callable=AsyncMock)
    async def test_retrieve_anti_monopoly(self, mock_get_db, mock_embed):
        mock_embed.return_value = [[0.5] * 128]
        chunks = [
            {"id": f"c{i}", "document_id": "d1", "heading": None, "content": f"text{i}",
             "offset_start": 0, "offset_end": 10, "score": 0.9 - i * 0.1}
            for i in range(5)
        ]
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[
            chunks,
            [{"id": "d1", "title": "Doc", "is_reference": False}],
        ])
        mock_get_db.return_value = mock_db

        result = await retrieve_context("proj1", "query", [], include_documents=True,
                                        include_references=False, include_memory=False)
        doc_hits = [h for h in result.hits if h.kind == "document"]
        assert len(doc_hits) <= 2

    @patch("retrieval.RETRIEVAL_BUDGET_TOKENS_DOCS", 1)
    @patch("retrieval.embed_texts", new_callable=AsyncMock)
    @patch("retrieval.get_db", new_callable=AsyncMock)
    async def test_retrieve_token_budget(self, mock_get_db, mock_embed):
        mock_embed.return_value = [[0.5] * 128]
        chunks = [
            {"id": f"c{i}", "document_id": f"d{i}", "heading": None, "content": "x" * 1000,
             "offset_start": 0, "offset_end": 1000, "score": 0.9}
            for i in range(5)
        ]
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[
            chunks,
            [{"id": f"d{i}", "title": f"Doc{i}", "is_reference": False} for i in range(5)],
        ])
        mock_get_db.return_value = mock_db

        result = await retrieve_context("proj1", "query", [], include_documents=True,
                                        include_references=False, include_memory=False)
        assert len(result.hits) < 5

    @patch.object(config, "RETRIEVAL_MIN_SCORE", 0.5)
    @patch("retrieval.embed_texts", new_callable=AsyncMock)
    @patch("retrieval.get_db", new_callable=AsyncMock)
    async def test_retrieve_min_score(self, mock_get_db, mock_embed):
        mock_embed.return_value = [[0.5] * 128]
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[
            [
                {"id": "c1", "document_id": "d1", "heading": None, "content": "low",
                 "offset_start": 0, "offset_end": 3, "score": 0.1},
            ],
            [{"id": "d1", "title": "Doc", "is_reference": False}],
        ])
        mock_get_db.return_value = mock_db

        result = await retrieve_context("proj1", "query", [], include_documents=True,
                                        include_references=False, include_memory=False)
        assert len(result.hits) == 0

    @patch("retrieval.embed_texts", new_callable=AsyncMock)
    @patch("retrieval.get_db", new_callable=AsyncMock)
    async def test_all_kinds_off_skips_embed_and_db(self, mock_get_db, mock_embed):
        # Every include flag false → nothing is retrievable; the embed call and the
        # DB round-trip are pure waste on that path and must not happen.
        result = await retrieve_context(
            "proj1", "query", [],
            include_documents=False, include_references=False, include_memory=False,
        )
        assert result.hits == []
        mock_embed.assert_not_called()
        mock_get_db.assert_not_awaited()

    @patch("retrieval.embed_texts", new_callable=AsyncMock, side_effect=EmbeddingConfigError("no config"))
    async def test_retrieve_config_error(self, mock_embed):
        result = await retrieve_context("proj1", "query", [], include_documents=True, include_references=True)
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

        result = await retrieve_context("proj1", "query", [], include_documents=True,
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


class TestQueryRewrite:
    async def test_query_rewrite_disabled(self):
        with patch("config.CHAT_QUERY_REWRITE_ENABLED", False):
            result = await _rewrite_query("hello", [{"role": "user", "content": "hi"}])
            assert result == "hello"

    async def test_query_rewrite_enabled(self, http_pool):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "rewritten query"}}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        http_pool("retrieval", mock_client)

        with patch("config.CHAT_QUERY_REWRITE_ENABLED", True):
            result = await _rewrite_query("hello", [{"role": "user", "content": "hi"}])
            assert result == "rewritten query"

    async def test_query_rewrite_failure(self, http_pool):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("API down"))
        http_pool("retrieval", mock_client)

        with patch("config.CHAT_QUERY_REWRITE_ENABLED", True):
            result = await _rewrite_query("hello", [{"role": "user", "content": "hi"}])
            assert result == "hello"
