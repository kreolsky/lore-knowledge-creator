"""PR2 R10: a `search_materials` layer failure is surfaced explicitly — never a
silent empty `hits` array. The model-facing result carries `warnings`/`error`;
the degraded search reports under `notice`, so the chip does not read it as a
failed call."""
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
async def _reset_db():
    from db import reset_db

    await reset_db()
    yield
    await reset_db()


async def test_search_materials_semantic_failure_is_surfaced():
    """Forcing the semantic layer to fail → result carries warnings + a non-empty
    error note (the model must not read an empty hits array as 'no results')."""
    from agent.readonly_executors import _search_materials_exec

    user = {"user_id": "u1"}
    with patch("retrieval.retrieve_context", new=AsyncMock(side_effect=RuntimeError("boom"))):
        # Direct layer: stub the DB queries to return empty candidates so the
        # direct layer succeeds cleanly and only the semantic layer degrades.
        async def fake_query(stmt, params=None):
            return []

        with patch("agent.search_exec.get_db") as gdb:
            gdb.return_value.query = fake_query
            result = await _search_materials_exec(
                project_id="p1", user=user, query="anything",
            )
    assert "search_degraded:semantic" in result["warnings"]
    assert result["notice"]  # non-empty note to the model, NOT an `error`


async def test_search_materials_semantic_graceful_error_is_surfaced():
    """retrieve_context returns a graceful RetrievalResult(error=...) WITHOUT raising
    (embedding pipeline broken / not configured). The `if not result.error:` block
    used to have no else → the layer contributed 0 hits silently. Now it appends a
    `search_degraded:semantic:<error>` warning."""
    from agent.readonly_executors import _search_materials_exec

    from retrieval import RetrievalResult

    user = {"user_id": "u1"}
    graceful = RetrievalResult(hits=[], error="embedding_failed")
    with patch("retrieval.retrieve_context", new=AsyncMock(return_value=graceful)):
        async def fake_query(stmt, params=None):
            return []

        with patch("agent.search_exec.get_db") as gdb:
            gdb.return_value.query = fake_query
            result = await _search_materials_exec(
                project_id="p1", user=user, query="anything",
            )
    assert "search_degraded:semantic:embedding_failed" in result["warnings"]
    assert result["notice"]
