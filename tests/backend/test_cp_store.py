"""Tests for cp_store: content-addressable blob store with dedup + compression.

# Pre-flight probe outcome (surrealdb==2.0.0): db.query() RAISES on errors —
# ValidationError for parse errors, ThrownError for runtime THROW. So the historical
# "silent blob loss" scenario (failed UPSERT returning the hash, checkpoint row
# written against a missing blob) does NOT occur today: put_content already raises.
# F1 is therefore defence-in-depth/hardening: it switches the UPSERT to query_raw +
# explicit per-statement status check so the failure mode is unambiguous across SDK
# upgrades (query_raw never raises; query() raising is a 2.0.0 contract that could
# revert). The stale 1.0.4-era "query() swallows DB errors" comment is gone.
"""


import pytest
import pytest_asyncio
from cp_store import get_content, hash_content, put_content


@pytest_asyncio.fixture
async def blob_store(test_db):
    await test_db.query("DELETE cp_blobs")
    yield
    await test_db.query("DELETE cp_blobs")


@pytest.mark.asyncio
async def test_put_and_get_round_trip(test_db, blob_store):
    text = "Hello world! " * 100
    ref = await put_content(text)
    assert ref == hash_content(text)
    resolved = await get_content(ref)
    assert resolved == text


@pytest.mark.asyncio
async def test_dedup_same_text_one_blob(test_db, blob_store):
    text = "Dedup test content " * 50
    ref1 = await put_content(text)
    ref2 = await put_content(text)
    assert ref1 == ref2

    rows = await test_db.query("SELECT count() AS c FROM cp_blobs GROUP ALL")
    assert rows and rows[0]["c"] == 1


@pytest.mark.asyncio
async def test_different_text_creates_different_blob(test_db, blob_store):
    ref1 = await put_content("Content A")
    ref2 = await put_content("Content B")
    assert ref1 != ref2

    rows = await test_db.query("SELECT count() AS c FROM cp_blobs GROUP ALL")
    assert rows and rows[0]["c"] == 2


@pytest.mark.asyncio
async def test_compression_reduces_size(test_db, blob_store):
    text = "Repetitive text. " * 1000
    ref = await put_content(text)

    rows = await test_db.query(
        "SELECT data, size FROM type::record('cp_blobs', $ref)",
        {"ref": ref},
    )
    assert rows
    stored_size = len(rows[0]["data"])
    original_size = len(text.encode("utf-8"))
    assert stored_size < original_size
    assert rows[0]["size"] == original_size


@pytest.mark.asyncio
async def test_get_content_missing_raises(blob_store):
    with pytest.raises(ValueError, match="not found"):
        await get_content("deadbeef" * 8)


@pytest.mark.asyncio
async def test_unicode_round_trip(test_db, blob_store):
    text = "Съешь ещё этих мягких французских булок 🥖 日本語テスト"
    ref = await put_content(text)
    assert await get_content(ref) == text


@pytest.mark.asyncio
async def test_empty_string_round_trip(test_db, blob_store):
    ref = await put_content("")
    assert await get_content(ref) == ""


# ─── F1: blob-write failure must surface, never produce a missing-blob checkpoint ──


@pytest.mark.asyncio
@pytest.mark.usefixtures("blob_store")
async def test_put_content_raises_when_blob_write_reports_err(monkeypatch):
    """F1: when the UPSERT layer reports an ERR status, put_content must raise
    RuntimeError instead of returning the hash (which would let create_checkpoint
    persist a checkpoint row pointing at a missing blob)."""
    from unittest.mock import AsyncMock

    import cp_store as mod

    fake_db = AsyncMock()
    # Fast-path SELECT returns falsy → falls through to the UPSERT.
    fake_db.query = AsyncMock(return_value=None)
    # query_raw returns a per-statement ERR (mirror run_in_transaction's shape).
    fake_db.query_raw = AsyncMock(
        return_value={"result": [{"status": "ERR", "result": "disk full"}]}
    )
    monkeypatch.setattr(mod, "get_db", AsyncMock(return_value=fake_db))

    with pytest.raises(RuntimeError):
        await put_content("important content that must not silently vanish")


@pytest.mark.asyncio
async def test_create_checkpoint_raises_when_blob_write_fails(monkeypatch, test_db):
    """F1 downstream: create_checkpoint must NOT persist a row when the blob write
    fails — no checkpoint may reference a missing blob (data-loss guard)."""
    from unittest.mock import AsyncMock

    import cp_store as mod

    fake_db = AsyncMock()
    fake_db.query = AsyncMock(return_value=None)
    fake_db.query_raw = AsyncMock(
        return_value={"result": [{"status": "ERR", "result": "write failed"}]}
    )
    monkeypatch.setattr(mod, "get_db", AsyncMock(return_value=fake_db))

    with pytest.raises(RuntimeError):
        await mod.create_checkpoint(
            document_id="d1", content="body", tables_json=None,
            label="v1", comment=None, created_by="u1",
        )
