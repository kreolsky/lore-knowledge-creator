"""Ordered migration runner."""

import pytest

from migrations.runner import run_migrations


async def _reset_migration_state():
    from db import get_db
    db = await get_db()
    await db.query("DELETE app_meta:migrations")


@pytest.mark.asyncio
async def test_runner_idempotent_on_second_run():
    """Second run: no migrations re-execute (all in applied set)."""
    await _reset_migration_state()
    from db import get_db
    db = await get_db()

    await run_migrations()
    rec1 = await db.query("SELECT applied FROM app_meta:migrations")
    applied1 = set(rec1[0].get("applied", []))

    await run_migrations()
    rec2 = await db.query("SELECT applied FROM app_meta:migrations")
    applied2 = set(rec2[0].get("applied", []))

    assert applied1 == applied2, "Second run should not add new applied entries"

    await _reset_migration_state()
