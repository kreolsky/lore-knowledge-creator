"""Tests for backplane and ydoc_store — Redis pub/sub, store load/replay/compact.

Redis backplane tests require a live Redis instance (DB 15, flushed between tests).
The ydoc_store tests mock the DB layer since the store wraps SurrealDB queries.
"""


import pytest
import pytest_asyncio
from backplane import get_backplane, reset_backplane


@pytest_asyncio.fixture
async def bp():
    """A RedisBackplane that closes its connections on teardown."""
    reset_backplane()
    instance = get_backplane()
    yield instance
    try:
        await instance.close()
    except Exception:
        pass
    reset_backplane()


class TestRedisBackplaneKV:
    @pytest.mark.asyncio
    async def test_set_then_get(self, bp):
        await bp.set("worker:code_stamp", "abc123")
        assert await bp.get("worker:code_stamp") == b"abc123"


class TestYdocStoreHelpers:
    """Tests for ydoc_store helper functions that don't need SurrealDB."""


    def test_public_api_names(self):
        from ydoc_store import get_text, seed_doc_from_content
        doc = seed_doc_from_content("Hello")
        assert str(get_text(doc)) == "Hello"

    def test_seed_doc_from_content_uses_deterministic_client(self):
        from ydoc_store import SEED_CLIENT_ID, seed_doc_from_content
        assert seed_doc_from_content("").client_id == SEED_CLIENT_ID
