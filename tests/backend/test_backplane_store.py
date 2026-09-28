"""Tests for backplane and ydoc_store — Redis pub/sub, store load/replay/compact.

Redis backplane tests require a live Redis instance (DB 15, flushed between tests).
The ydoc_store tests mock the DB layer since the store wraps SurrealDB queries.
"""

import asyncio

import pytest
import pytest_asyncio
from backplane import RedisBackplane, get_backplane, reset_backplane
from pycrdt import Doc, Text


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


class TestRedisBackplane:
    @pytest.mark.asyncio
    async def test_publish_subscribe(self, bp):
        received: list[bytes] = []

        async def handler(data: bytes):
            received.append(data)

        await bp.subscribe("ydoc:doc1", handler)
        await bp.publish("ydoc:doc1", b"hello")
        await asyncio.sleep(0.1)

        assert received == [b"hello"]

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, bp):
        a: list[bytes] = []
        b: list[bytes] = []

        await bp.subscribe("ch", lambda d: a.append(d))
        await bp.subscribe("ch", lambda d: b.append(d))
        await bp.publish("ch", b"data")
        await asyncio.sleep(0.1)

        assert a == [b"data"]
        assert b == [b"data"]

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self, bp):
        received: list[bytes] = []

        await bp.subscribe("ch", lambda d: received.append(d))
        await bp.publish("ch", b"first")
        await asyncio.sleep(0.1)
        assert received == [b"first"]

        await bp.unsubscribe("ch")
        await bp.publish("ch", b"second")
        await asyncio.sleep(0.1)
        assert received == [b"first"]

    @pytest.mark.asyncio
    async def test_channel_isolation(self, bp):
        a: list[bytes] = []
        b: list[bytes] = []

        await bp.subscribe("ch_a", lambda d: a.append(d))
        await bp.subscribe("ch_b", lambda d: b.append(d))
        await bp.publish("ch_a", b"for_a")
        await asyncio.sleep(0.1)

        assert a == [b"for_a"]
        assert b == []

    @pytest.mark.asyncio
    async def test_two_replica_fanout(self, bp):
        doc_a = Doc()
        doc_b = Doc()
        text_a = doc_a.get("content", type=Text)
        text_b = doc_b.get("content", type=Text)

        updates_b: list[bytes] = []

        async def handler_b(data: bytes):
            updates_b.append(data)

        await bp.subscribe("ydoc:doc1", handler_b)

        text_a += "Hello"
        update = doc_a.get_update()
        await bp.publish("ydoc:doc1", update)
        await asyncio.sleep(0.1)

        for u in updates_b:
            doc_b.apply_update(u)

        assert str(text_b) == "Hello"

    @pytest.mark.asyncio
    async def test_concurrent_edits_converge_via_backplane(self, bp):
        doc_a = Doc()
        doc_b = Doc()
        text_a = doc_a.get("content", type=Text)
        text_b = doc_b.get("content", type=Text)
        text_a += "base"
        text_b += "base"

        updates_a: list[bytes] = []
        updates_b: list[bytes] = []

        await bp.subscribe("ydoc:doc1", lambda d: updates_a.append(d))
        await bp.subscribe("ydoc:doc1", lambda d: updates_b.append(d))

        text_a += " from A"
        text_b += " from B"

        await bp.publish("ydoc:doc1", doc_a.get_update())
        await bp.publish("ydoc:doc1", doc_b.get_update())
        await asyncio.sleep(0.2)

        for u in updates_b:
            doc_b.apply_update(u)
        for u in updates_a:
            doc_a.apply_update(u)

        assert str(text_a) == str(text_b)


class TestRedisBackplaneKV:
    @pytest.mark.asyncio
    async def test_set_then_get(self, bp):
        await bp.set("worker:code_stamp", "abc123")
        assert await bp.get("worker:code_stamp") == b"abc123"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, bp):
        assert await bp.get("worker:nonexistent") is None


class TestGetBackplane:
    def test_returns_redis(self):
        reset_backplane()
        bp = get_backplane()
        assert isinstance(bp, RedisBackplane)

    def test_singleton(self):
        reset_backplane()
        bp1 = get_backplane()
        bp2 = get_backplane()
        assert bp1 is bp2


class TestYdocStoreHelpers:
    """Tests for ydoc_store helper functions that don't need SurrealDB."""

    def test_doc_from_snapshot_empty(self):
        from ydoc_store import _doc_from_snapshot, get_text
        doc = _doc_from_snapshot(None)
        text = get_text(doc)
        assert str(text) == ""

    def test_doc_from_snapshot_with_content(self):
        from ydoc_store import _doc_from_snapshot, get_text
        source = Doc()
        source.get("content", type=Text)
        source_text = source.get("content", type=Text)
        source_text += "Hello"
        snapshot = source.get_update()

        doc = _doc_from_snapshot(snapshot)
        text = get_text(doc)
        assert str(text) == "Hello"

    def test_get_text_returns_ytext(self):
        from ydoc_store import _doc_from_snapshot, get_text
        doc = _doc_from_snapshot(None)
        t = get_text(doc)
        assert isinstance(t, Text)

    def test_public_api_names(self):
        from ydoc_store import get_text, seed_doc_from_content
        doc = seed_doc_from_content("Hello")
        assert str(get_text(doc)) == "Hello"

    def test_seed_doc_from_content_uses_deterministic_client(self):
        from ydoc_store import SEED_CLIENT_ID, seed_doc_from_content
        assert seed_doc_from_content("").client_id == SEED_CLIENT_ID
