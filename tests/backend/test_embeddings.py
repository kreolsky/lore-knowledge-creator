"""Unit tests for embeddings module — mocked DB and API."""

import asyncio
from unittest.mock import AsyncMock, patch

from embeddings import (
    _reembed,
    embed_texts,
)


class TestEmbedTexts:
    async def test_embed_texts_empty(self):
        assert await embed_texts([]) == []


class TestReembed:

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


class TestEmbeddingDegradedSignal:
    """Per-project sticky degraded/recovered events on embed failure/success."""

    def _reset_state(self, emb):
        emb._consecutive_embed_failures.clear()
        emb._degraded_projects.clear()

    async def _drive_failure(self, emb, project_id: str):
        await emb._on_embed_failure(project_id)

    async def _drive_success(self, emb, project_id: str):
        await emb._on_embed_success(project_id)


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
