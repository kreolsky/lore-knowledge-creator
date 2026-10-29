"""Tests for per-site query timing aggregation and the timed DB proxy."""

from unittest.mock import AsyncMock

import pytest
import query_stats


@pytest.mark.asyncio
async def test_timed_db_proxy_records_site_and_returns_result():
    """_TimedDB.query times the call, records under the site label, returns the result."""
    from db import _TimedDB

    query_stats.reset_for_test()
    fake_conn = AsyncMock()
    fake_conn.query = AsyncMock(return_value=[{"id": "ok"}])

    proxy = _TimedDB(fake_conn)
    result = await proxy.query("SELECT 1", {"x": 1}, site="collab_flush")

    assert result == [{"id": "ok"}]
    fake_conn.query.assert_awaited_once_with("SELECT 1", {"x": 1})
    assert query_stats.snapshot()["collab_flush"]["count"] == 1
