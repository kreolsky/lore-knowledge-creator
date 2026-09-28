"""Unit tests for embeddings module — mocked DB and API."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from helpers import pinned_embedding_api

from embeddings import (
    EmbeddingConfigError,
    _reembed,
    embed_texts,
)


class TestEmbedTexts:
    async def test_embed_texts_empty(self):
        assert await embed_texts([]) == []

    @patch("embeddings._ensure_config", new_callable=AsyncMock)
    async def test_embed_texts_batch(self, mock_config, http_pool):
        mock_config.return_value = None
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 1, "embedding": [0.3, 0.4]},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        http_pool("embeddings", mock_client)

        result = await embed_texts(["hello", "world"])
        assert result == [[0.1, 0.2], [0.3, 0.4]]

    @patch("config.EMBEDDING_BATCH_SIZE", 2)
    @patch("embeddings._ensure_config", new_callable=AsyncMock)
    async def test_embed_texts_batching(self, mock_config, http_pool):
        mock_config.return_value = None
        texts = [f"text{i}" for i in range(5)]
        call_count = 0

        async def mock_post(url, **kwargs):
            nonlocal call_count
            call_count += 1
            batch_input = kwargs["json"]["input"]
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "data": [
                    {"index": j, "embedding": [float(j)]}
                    for j in range(len(batch_input))
                ]
            }
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        mock_client = AsyncMock()
        mock_client.post = mock_post
        http_pool("embeddings", mock_client)

        result = await embed_texts(texts)
        assert len(result) == 5
        assert call_count == 3


class TestEnsureConfig:
    async def test_config_error_no_url(self):
        import embeddings as emb
        with pinned_embedding_api(""):
            with pytest.raises(EmbeddingConfigError, match="EMBEDDING_API_URL"):
                await emb._ensure_config()



class TestDebounce:
    async def test_debounce_enqueues_embed_task(self, enqueue_recorder):
        import embeddings as emb

        class FakeRedis:
            async def set(self, key, value, **kwargs):
                pass

        with patch("jobs.pool.get_arq_pool", return_value=FakeRedis()):
            await emb._on_content_flushed("doc", "test-debounce-1", "proj1")

        calls = enqueue_recorder.calls
        assert len(calls) == 1
        assert calls[0][0] == "embed_document_task"
        assert calls[0][2]["job_id"] == "embed:test-debounce-1"

    async def test_debounce_second_call_updates_deadline(self, enqueue_recorder):
        import embeddings as emb

        set_calls = []

        class FakeRedis:
            async def set(self, key, value, **kwargs):
                set_calls.append((key, float(value), kwargs))

        with patch("jobs.pool.get_arq_pool", return_value=FakeRedis()):
            await emb._on_content_flushed("doc", "test-debounce-2", "proj1")
            await emb._on_content_flushed("doc", "test-debounce-2", "proj1")

        assert len(enqueue_recorder.calls) == 2
        assert len(set_calls) == 2
        assert set_calls[1][1] > set_calls[0][1], "Second deadline should be later"


class TestReembed:
    @patch("embeddings.embed_texts", new_callable=AsyncMock)
    @patch("embeddings.get_db", new_callable=AsyncMock)
    @patch("embeddings._ensure_config", new_callable=AsyncMock)
    async def test_reembed_doc(self, mock_config, mock_get_db, mock_embed):
        mock_config.return_value = None
        mock_embed.return_value = [[0.1] * 128]
        mock_db = AsyncMock()
        # query order: SELECT parent → SELECT existing chunks (S3 reuse read) →
        # UPDATE content_version → UPDATE embedding_status. The DELETE-orphan +
        # CREATE + in-txn CAS run via run_in_transaction (query_raw).
        mock_db.query = AsyncMock(side_effect=[
            [{"content": "## Hello\nWorld", "content_version": 0, "title": "Doc"}],
            [],   # no existing chunks → embed all
            None,
            None,
        ])
        mock_db.query_raw = AsyncMock(return_value={"result": [{"status": "OK"}]})
        mock_get_db.return_value = mock_db

        await _reembed("doc", "doc1", "proj1")
        assert mock_db.query.call_count == 4
        assert mock_db.query_raw.call_count == 1

    @patch("embeddings.embed_texts", new_callable=AsyncMock)
    @patch("embeddings.get_db", new_callable=AsyncMock)
    @patch("embeddings._ensure_config", new_callable=AsyncMock)
    async def test_reembed_ref(self, mock_config, mock_get_db, mock_embed):
        # After unification: refs are documents with is_reference=true, embedded
        # via the same doc_chunks path. entity_type="ref" is still accepted as a
        # legacy alias and routed through the doc pipeline.
        mock_config.return_value = None
        mock_embed.return_value = [[0.2] * 128]
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[
            [{"content": "## Ref body\nText", "content_version": 0, "title": "Ref"}],
            [],   # no existing chunks → embed all
            None,
            None,
        ])
        mock_db.query_raw = AsyncMock(return_value={"result": [{"status": "OK"}]})
        mock_get_db.return_value = mock_db

        await _reembed("ref", "ref1", "proj1")
        assert mock_db.query.call_count == 4
        assert mock_db.query_raw.call_count == 1

    @patch("embeddings.embed_texts", new_callable=AsyncMock)
    @patch("embeddings.get_db", new_callable=AsyncMock)
    @patch("embeddings._ensure_config", new_callable=AsyncMock)
    async def test_reembed_unknown_entity_noop(self, mock_config, mock_get_db, mock_embed):
        # Unknown entity_type — short-circuit before any DB or embedding call.
        mock_config.return_value = None
        mock_db = AsyncMock()
        mock_db.query = AsyncMock()
        mock_get_db.return_value = mock_db

        await _reembed("note", "n1", "proj1")
        assert mock_db.query.call_count == 0
        assert mock_embed.call_count == 0


class TestEmbeddingDegradedSignal:
    """Per-project sticky degraded/recovered events on embed failure/success."""

    def _reset_state(self, emb):
        emb._consecutive_embed_failures.clear()
        emb._degraded_projects.clear()

    async def _drive_failure(self, emb, project_id: str):
        await emb._on_embed_failure(project_id)

    async def _drive_success(self, emb, project_id: str):
        await emb._on_embed_success(project_id)

    async def test_third_failure_fires_degraded_once(self):
        import embeddings as emb
        from event_bus import off as bus_off
        from event_bus import on as bus_on
        self._reset_state(emb)

        events: list[dict] = []

        async def listener(**kwargs):
            events.append(kwargs)

        bus_on("embedding_degraded", listener)
        try:
            for _ in range(3):
                await self._drive_failure(emb, "p1")
            await asyncio.sleep(0)
            assert len(events) == 1
            assert events[0]["project_id"] == "p1"

            await self._drive_failure(emb, "p1")
            await asyncio.sleep(0)
            assert len(events) == 1
        finally:
            bus_off("embedding_degraded", listener)
            self._reset_state(emb)

    async def test_success_after_degraded_fires_recovered(self):
        import embeddings as emb
        from event_bus import off as bus_off
        from event_bus import on as bus_on
        self._reset_state(emb)

        recovered: list[dict] = []

        async def listener(**kwargs):
            recovered.append(kwargs)

        bus_on("embedding_recovered", listener)
        try:
            for _ in range(3):
                await self._drive_failure(emb, "p1")
            await self._drive_success(emb, "p1")
            await asyncio.sleep(0)
            assert len(recovered) == 1
            assert recovered[0]["project_id"] == "p1"
            assert "p1" not in emb._degraded_projects
            assert emb._consecutive_embed_failures.get("p1", 0) == 0
        finally:
            bus_off("embedding_recovered", listener)
            self._reset_state(emb)

    async def test_success_before_threshold_resets_counter_silently(self):
        import embeddings as emb
        from event_bus import off as bus_off
        from event_bus import on as bus_on
        self._reset_state(emb)

        degraded: list[dict] = []
        recovered: list[dict] = []

        async def deg(**kwargs):
            degraded.append(kwargs)

        async def rec(**kwargs):
            recovered.append(kwargs)

        bus_on("embedding_degraded", deg)
        bus_on("embedding_recovered", rec)
        try:
            await self._drive_failure(emb, "p1")
            await self._drive_failure(emb, "p1")
            await self._drive_success(emb, "p1")
            await asyncio.sleep(0)
            assert degraded == []
            assert recovered == []
            assert emb._consecutive_embed_failures.get("p1", 0) == 0
        finally:
            bus_off("embedding_degraded", deg)
            bus_off("embedding_recovered", rec)
            self._reset_state(emb)

    async def test_failures_in_different_projects_are_independent(self):
        import embeddings as emb
        from event_bus import off as bus_off
        from event_bus import on as bus_on
        self._reset_state(emb)

        events: list[dict] = []

        async def listener(**kwargs):
            events.append(kwargs)

        bus_on("embedding_degraded", listener)
        try:
            for _ in range(3):
                await self._drive_failure(emb, "pA")
            for _ in range(2):
                await self._drive_failure(emb, "pB")
            await asyncio.sleep(0)
            assert [e["project_id"] for e in events] == ["pA"]

            await self._drive_failure(emb, "pB")
            await asyncio.sleep(0)
            assert [e["project_id"] for e in events] == ["pA", "pB"]
        finally:
            bus_off("embedding_degraded", listener)
            self._reset_state(emb)


class TestEmbeddingStatus:
    """Persistent retry flag: embedding_status transitions via _reembed."""

    @staticmethod
    def _reset_state(emb):
        emb._consecutive_embed_failures.clear()
        emb._degraded_projects.clear()

    @patch("embeddings.embed_texts", new_callable=AsyncMock, side_effect=RuntimeError("provider down"))
    @patch("embeddings.get_db", new_callable=AsyncMock)
    @patch("embeddings._ensure_config", new_callable=AsyncMock)
    async def test_ok_to_failed_on_exception(self, mock_config, mock_get_db, mock_embed):
        mock_config.return_value = None
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[
            [{"content": "## Test\nBody", "content_version": 0}],
            None,
        ])
        mock_get_db.return_value = mock_db

        import embeddings as emb
        self._reset_state(emb)
        # embed_texts raises before the transaction — only the parent SELECT runs.
        with pytest.raises(RuntimeError):
            await emb._reembed("doc", "doc1", "proj1")

    @patch("embeddings.embed_texts", new_callable=AsyncMock, return_value=[[0.1] * 128])
    @patch("embeddings.get_db", new_callable=AsyncMock)
    @patch("embeddings._ensure_config", new_callable=AsyncMock)
    async def test_failed_to_ok_on_success(self, mock_config, mock_get_db, mock_embed):
        mock_config.return_value = None
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[
            [{"content": "## Test\nBody", "content_version": 0, "title": "T"}],
            [],   # no existing chunks → embed all
            None,
            None,
        ])
        mock_db.query_raw = AsyncMock(return_value={"result": [{"status": "OK"}]})
        mock_get_db.return_value = mock_db

        import embeddings as emb
        self._reset_state(emb)
        await emb._reembed("doc", "doc1", "proj1")

        last_query = mock_db.query.call_args_list[-1][0][0]
        assert "embedding_status" in last_query
        assert "ok" in last_query
