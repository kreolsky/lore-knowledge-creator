"""Spike: verify zstd-compressed bytes survive surrealdb-py write/read round-trip.

Nothing in this codebase currently stores binary via the SDK's WS RPC. Phase 1
needs cp_blobs.data to hold compressed blobs. This spike confirms the transport
preserves bytes exactly — or decides that data must be stored as base64 string.

Two variants tested:
  1. SurrealDB field TYPE string  + base64-encoded payload (safe, proven).
  2. SurrealDB field TYPE bytes   + raw bytes payload      (ideal, unproven).

If variant 2 fails (CBOR mangles bytes or SurrealDB rejects the type), variant 1
becomes the decision and the schema uses `TYPE string` for cp_blobs.data.
"""

import base64
import hashlib

import pytest
import pytest_asyncio
import zstandard

_SAMPLE_TEXT = (
    "The quick brown fox jumps over the lazy dog. " * 500
    + "Съешь ещё этих мягких французских булок, да выпей чаю. " * 100
    + "日本語のテスト 🎉🚀\n"
)


def _compress(text: str) -> bytes:
    return zstandard.compress(text.encode("utf-8"))


def _decompress(data: bytes) -> str:
    return zstandard.decompress(data).decode("utf-8")


@pytest_asyncio.fixture
async def blob_table(test_db):
    await test_db.query("DEFINE TABLE IF NOT EXISTS cp_blob_spike SCHEMAFULL")
    await test_db.query("DEFINE FIELD IF NOT EXISTS data_b64 ON cp_blob_spike TYPE string")
    yield
    await test_db.query("DELETE cp_blob_spike")


@pytest_asyncio.fixture
async def blob_bytes_table(test_db):
    await test_db.query("DEFINE TABLE IF NOT EXISTS cp_blob_bytes_spike SCHEMAFULL")
    try:
        await test_db.query("DEFINE FIELD IF NOT EXISTS data_raw ON cp_blob_bytes_spike TYPE bytes")
    except Exception as exc:
        pytest.skip(f"SurrealDB does not support TYPE bytes: {exc}")
        return
    yield
    await test_db.query("DELETE cp_blob_bytes_spike")


class TestBase64RoundTrip:
    async def test_base64_round_trip(self, test_db, blob_table):
        compressed = _compress(_SAMPLE_TEXT)
        b64 = base64.b64encode(compressed).decode("ascii")
        original_hash = hashlib.sha256(compressed).hexdigest()

        await test_db.query(
            "CREATE cp_blob_spike:test_b64 CONTENT { data_b64: $data }",
            {"data": b64},
        )

        rows = await test_db.query("SELECT data_b64 FROM cp_blob_spike:test_b64")
        assert rows and len(rows) == 1
        stored_b64 = rows[0]["data_b64"]
        assert isinstance(stored_b64, str)

        restored_compressed = base64.b64decode(stored_b64)
        assert hashlib.sha256(restored_compressed).hexdigest() == original_hash
        assert _decompress(restored_compressed) == _SAMPLE_TEXT


class TestRawBytesRoundTrip:
    async def test_raw_bytes_round_trip(self, test_db, blob_bytes_table):
        compressed = _compress(_SAMPLE_TEXT)
        original_hash = hashlib.sha256(compressed).hexdigest()

        await test_db.query(
            "CREATE cp_blob_bytes_spike:test_raw CONTENT { data_raw: $data }",
            {"data": compressed},
        )

        rows = await test_db.query("SELECT data_raw FROM cp_blob_bytes_spike:test_raw")
        assert rows and len(rows) == 1
        stored = rows[0]["data_raw"]

        if isinstance(stored, str):
            pytest.skip(
                "SurrealDB returned bytes as string — raw bytes not preserved. "
                "Decision: cp_blobs.data = base64 string."
            )

        assert isinstance(stored, bytes)
        assert hashlib.sha256(stored).hexdigest() == original_hash
        assert _decompress(stored) == _SAMPLE_TEXT


class TestCompressionRatio:
    def test_zstd_ratio_on_prose(self):
        text = _SAMPLE_TEXT
        compressed = _compress(text)
        ratio = len(text.encode("utf-8")) / len(compressed)
        assert ratio > 2.0, f"Expected >2x compression, got {ratio:.1f}x"
