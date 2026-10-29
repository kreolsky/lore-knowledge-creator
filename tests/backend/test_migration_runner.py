"""4.3 — Ordered migration runner.

The runner replaces ad-hoc marker checks in main.py with a centralised registry.
Backward compat: legacy per-migration markers (app_meta:sort_keys_backfill,
app_meta:ref_mentions_backfill) fold in as already-applied on first boot.
"""

import pytest

from migrations.runner import run_migrations


async def _reset_migration_state():
    from db import get_db
    db = await get_db()
    await db.query("DELETE app_meta:migrations")
    await db.query("DELETE app_meta:sort_keys_backfill")
    await db.query("DELETE app_meta:ref_mentions_backfill")


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


@pytest.mark.asyncio
async def test_legacy_markers_fold_in_as_applied():
    """If legacy markers exist (pre-runner DB), those migrations are pre-applied."""
    await _reset_migration_state()
    from db import get_db
    db = await get_db()
    await db.query("CREATE app_meta:sort_keys_backfill SET done_at = time::now()")
    await db.query("CREATE app_meta:ref_mentions_backfill SET done_at = time::now()")

    await run_migrations()

    record = await db.query("SELECT applied FROM app_meta:migrations")
    applied = set(record[0].get("applied", []))
    assert "sort_keys_backfill" in applied
    assert "ref_mentions_backfill" in applied

    await _reset_migration_state()
