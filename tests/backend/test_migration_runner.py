"""4.3 — Ordered migration runner.

The runner replaces ad-hoc marker checks in main.py with a centralised registry.
Backward compat: legacy per-migration markers (app_meta:sort_keys_backfill,
app_meta:ref_mentions_backfill) fold in as already-applied on first boot.
"""

import pytest

from migrations.runner import _MIGRATIONS, run_migrations


async def _reset_migration_state():
    from db import get_db
    db = await get_db()
    await db.query("DELETE app_meta:migrations")
    await db.query("DELETE app_meta:sort_keys_backfill")
    await db.query("DELETE app_meta:ref_mentions_backfill")


@pytest.mark.asyncio
async def test_runner_has_registered_migrations():
    """The runner must register the two not-yet-everywhere migrations.

    Entries are retired (module + registry line deleted) once they have run on
    every installation; these two have not reached every installation yet.
    """
    names = [name for name, _ in _MIGRATIONS]
    assert "messages_schemafull_converge" in names
    assert "chat_apply_lock_fields_drop" in names


def test_migrations_registry_order_snapshot():
    """Pin the exact ordered registry — the load-bearing contract.

    Block 2 split runner.py into one module per migration; the #1 risk is a
    silent registry reorder/drop that skips or mis-orders a migration on boot.
    This snapshot guards the order verbatim; any re-import that changes the
    sequence fails here, not in prod data.
    """
    names = [name for name, _ in _MIGRATIONS]
    assert names == [
        "messages_schemafull_converge",
        "chat_apply_lock_fields_drop",
        "chat_lifecycle_drop",
        "document_access_drop",
        "comfy_config_docs_drop",
        "reference_sort_keys_backfill",
        "web_search_off_drop",
        "turn_timeout_override_fold",
    ]
    # Every entry resolves to a distinct callable (a lost import would leave a
    # duplicated/None fn but keep the name).
    fns = [fn for _, fn in _MIGRATIONS]
    assert all(callable(fn) for fn in fns)
    assert len({id(fn) for fn in fns}) == len(fns)


@pytest.mark.asyncio
async def test_runner_applies_migrations_and_records():
    """First run: all migrations apply and are recorded in app_meta:migrations."""
    await _reset_migration_state()
    from db import get_db
    db = await get_db()

    await run_migrations()

    record = await db.query("SELECT applied FROM app_meta:migrations")
    assert record, "app_meta:migrations record should exist after run"
    applied = set(record[0].get("applied", []))
    for name, _ in _MIGRATIONS:
        assert name in applied, f"Migration {name} not recorded as applied"

    await _reset_migration_state()


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
