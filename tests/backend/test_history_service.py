"""Tests for document history service — create, idempotency, list."""

import pytest

from db import create_record


async def _seed_project(project_id: str) -> None:
    await create_record("projects", project_id, {
        "name": "HistTest",
        "status": "active",
        "project_context": "",
    })


@pytest.mark.asyncio
async def test_log_first_edit_repeat_logs_no_error(test_db, caplog):
    """A repeat edit is the expected outcome, not a DB failure — it must not log ERROR."""
    from history_service import list_history, log_first_edit
    await _seed_project("hp5")
    await create_record("documents", "hd5", {
        "project_id": "hp5", "title": "D", "content": "", "path": "d.md",
    })
    await log_first_edit("hd5", "u1", "Alice")
    caplog.clear()
    with caplog.at_level("DEBUG"):
        assert await log_first_edit("hd5", "u1", "Alice") is None
    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == []
    assert len(await list_history("hd5")) == 1
