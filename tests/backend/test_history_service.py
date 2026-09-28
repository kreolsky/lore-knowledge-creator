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
async def test_log_event_creates_entry(test_db):
    from history_service import log_event
    await _seed_project("hp1")
    await create_record("documents", "hd1", {
        "project_id": "hp1", "title": "D", "content": "", "path": "d.md",
    })
    result = await log_event("hd1", "u1", "Alice", "created")
    assert result is not None
    assert result["document_id"] == "hd1"
    assert result["user_name"] == "Alice"
    assert result["action"] == "created"


@pytest.mark.asyncio
async def test_log_first_edit_idempotent(test_db):
    from history_service import log_first_edit
    await _seed_project("hp2")
    await create_record("documents", "hd2", {
        "project_id": "hp2", "title": "D", "content": "", "path": "d.md",
    })
    r1 = await log_first_edit("hd2", "u1", "Alice")
    assert r1 is not None
    assert r1["action"] == "edited"
    r2 = await log_first_edit("hd2", "u1", "Alice")
    assert r2 is None


@pytest.mark.asyncio
async def test_log_first_edit_different_users(test_db):
    from history_service import log_first_edit
    await _seed_project("hp3")
    await create_record("documents", "hd3", {
        "project_id": "hp3", "title": "D", "content": "", "path": "d.md",
    })
    r1 = await log_first_edit("hd3", "u1", "Alice")
    r2 = await log_first_edit("hd3", "u2", "Bob")
    assert r1 is not None
    assert r2 is not None


@pytest.mark.asyncio
async def test_list_history_ordered(test_db):
    from history_service import list_history, log_event
    await _seed_project("hp4")
    await create_record("documents", "hd4", {
        "project_id": "hp4", "title": "D", "content": "", "path": "d.md",
    })
    await log_event("hd4", "u1", "Alice", "created")
    await log_event("hd4", "u2", "Bob", "edited")
    rows = await list_history("hd4")
    assert len(rows) == 2
    assert rows[0]["action"] == "created"
    assert rows[1]["action"] == "edited"
