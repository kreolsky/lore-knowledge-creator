"""Unit tests for pure functions in db.py, plus integration tests for fetch_many."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from db import (
    _fmt_datetime,
    extract_id,
    fetch_many,
)


class TestExtractId:


    def test_recordid_repr(self):
        s = "RecordID(table_name=users, record_id=`abc-123`)"
        assert extract_id(s) == "abc-123"


class TestFmtDatetime:


    def test_invalid_string(self):
        assert _fmt_datetime("not-a-date") is None

    def test_integer(self):
        assert _fmt_datetime(12345) is None


# ─── fetch_many integration tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_many_empty_input(test_db):
    """fetch_many with empty list returns empty dict without DB call."""
    result = await fetch_many("users", [])
    assert result == {}


# ─── DBPool unit tests ──────────────────────────────────────────────────────


class TestDBPool:
    def _make_fresh_pool(self):
        from db import DBPool
        pool = DBPool.__new__(DBPool)
        pool._db = None
        pool._db_lock = asyncio.Lock()
        pool._last_check_ts = 0.0
        pool._startup_complete = False
        return pool


    @pytest.mark.asyncio
    async def test_reset_db_clears_connection(self):
        pool = self._make_fresh_pool()
        mock_conn = AsyncMock()
        pool._db = mock_conn
        pool._last_check_ts = time.monotonic()
        await pool.reset_db()
        assert pool._db is None

    def test_mark_startup_complete(self):
        pool = self._make_fresh_pool()
        assert pool._startup_complete is False
        pool.mark_startup_complete()
        assert pool._startup_complete is True


# ─── ConfigError unit tests ─────────────────────────────────────────────────


class TestConfigError:

    def test_require_env_raises_config_error(self):
        from config import _require_env
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError):
                _require_env("NONEXISTENT_VAR_12345")
