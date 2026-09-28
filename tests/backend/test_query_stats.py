"""Tests for per-site query timing aggregation and the timed DB proxy."""

from unittest.mock import AsyncMock

import pytest
import query_stats


@pytest.mark.asyncio
async def test_record_query_aggregates_per_site():
    query_stats.reset_for_test()
    query_stats.record_query("collab_flush", 10.0)
    query_stats.record_query("collab_flush", 30.0)
    query_stats.record_query("collab_flush", 20.0)
    snap = query_stats.snapshot()
    site = snap["collab_flush"]
    assert site["count"] == 3
    assert site["total_ms"] == 60.0
    assert site["max_ms"] == 30.0


@pytest.mark.asyncio
async def test_record_query_separates_sites():
    query_stats.reset_for_test()
    query_stats.record_query("collab_flush", 5.0)
    query_stats.record_query("access_check", 7.0)
    snap = query_stats.snapshot()
    assert snap["collab_flush"]["count"] == 1
    assert snap["access_check"]["count"] == 1
    assert snap["access_check"]["max_ms"] == 7.0


@pytest.mark.asyncio
async def test_snapshot_is_unspecified_when_no_site_label():
    query_stats.reset_for_test()
    query_stats.record_query("unspecified", 1.0)
    assert "unspecified" in query_stats.snapshot()


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


@pytest.mark.asyncio
async def test_timed_db_proxy_defaults_to_unspecified_site():
    from db import _TimedDB

    query_stats.reset_for_test()
    fake_conn = AsyncMock()
    fake_conn.query = AsyncMock(return_value=[])

    proxy = _TimedDB(fake_conn)
    await proxy.query("RETURN 1")

    assert query_stats.snapshot()["unspecified"]["count"] == 1
    fake_conn.query.assert_awaited_once_with("RETURN 1", None)


@pytest.mark.asyncio
async def test_timed_db_proxy_query_raw_timed():
    from db import _TimedDB

    query_stats.reset_for_test()
    fake_conn = AsyncMock()
    fake_conn.query_raw = AsyncMock(return_value={"result": []})

    proxy = _TimedDB(fake_conn)
    await proxy.query_raw("SELECT * FROM x", None, site="ydoc_compact")

    assert query_stats.snapshot()["ydoc_compact"]["count"] == 1
    fake_conn.query_raw.assert_awaited_once_with("SELECT * FROM x", None)


@pytest.mark.asyncio
async def test_timed_db_proxy_delegates_other_attrs():
    from db import _TimedDB

    fake_conn = AsyncMock()
    fake_conn.use = AsyncMock()
    proxy = _TimedDB(fake_conn)
    await proxy.use("ns", "db")
    fake_conn.use.assert_awaited_once_with("ns", "db")
