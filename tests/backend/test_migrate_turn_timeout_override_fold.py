"""turn_timeout_override_fold: a TURN_TIMEOUT_S override survives the key's removal.

TURN_TIMEOUT_S stopped being a setting; an override stored under it would be
ignored silently. The migration carries it onto TURN_PROGRESS_GRACE_S unless
the grace already has its own, and deletes the old row either way.
"""

import json

import pytest

from migrations.migrate_turn_timeout_override_fold import (
    _migrate_turn_timeout_override_fold,
)


@pytest.fixture(autouse=True)
def _pinned_default(monkeypatch):
    """The grace's default, pinned: settings.get prefers an env-present key and
    config holds the import-time value, so the runner's env must not answer."""
    import config

    monkeypatch.delenv("TURN_PROGRESS_GRACE_S", raising=False)
    monkeypatch.setattr(config, "TURN_PROGRESS_GRACE_S", 300.0)


@pytest.fixture
async def clean_db(test_db):
    """No TURN rows before or after: `test_db` alone does not clean
    instance_settings between tests (the `client` fixture does), and a leftover
    grace row would outrank the config patches of the channel tests."""
    clear = "DELETE instance_settings WHERE key IN ['TURN_TIMEOUT_S', 'TURN_PROGRESS_GRACE_S']"
    await test_db.query(clear)
    yield test_db
    await test_db.query(clear)
    import settings

    settings.drop_cache()


async def _put_override(test_db, key: str, value: float) -> None:
    await test_db.query(
        "UPSERT type::record('instance_settings', $key) SET "
        "key = $key, value = $value, updated_by = $by, updated_at = time::now()",
        {"key": key, "value": json.dumps(value), "by": "migration-test"},
    )


async def _keys(test_db) -> set[str]:
    rows = await test_db.query(
        "SELECT key FROM instance_settings WHERE key IN ['TURN_TIMEOUT_S', 'TURN_PROGRESS_GRACE_S']")
    return {row["key"] for row in rows or []}


@pytest.mark.asyncio
async def test_old_override_reaches_the_grace(clean_db):
    test_db = clean_db
    import settings

    await _put_override(test_db, "TURN_TIMEOUT_S", 600.0)
    await _migrate_turn_timeout_override_fold(test_db)
    settings.drop_cache()
    assert await settings.get("TURN_PROGRESS_GRACE_S") == 600.0
    assert "TURN_TIMEOUT_S" not in await _keys(test_db)


@pytest.mark.asyncio
async def test_the_grace_own_override_wins(clean_db):
    test_db = clean_db
    import settings

    await _put_override(test_db, "TURN_TIMEOUT_S", 600.0)
    await _put_override(test_db, "TURN_PROGRESS_GRACE_S", 120.0)
    await _migrate_turn_timeout_override_fold(test_db)
    settings.drop_cache()
    assert await settings.get("TURN_PROGRESS_GRACE_S") == 120.0
    assert "TURN_TIMEOUT_S" not in await _keys(test_db)


@pytest.mark.asyncio
async def test_no_override_and_a_rerun_are_noops(clean_db):
    test_db = clean_db
    import settings

    await _migrate_turn_timeout_override_fold(test_db)
    await _put_override(test_db, "TURN_TIMEOUT_S", 600.0)
    await _migrate_turn_timeout_override_fold(test_db)
    await _migrate_turn_timeout_override_fold(test_db)
    settings.drop_cache()
    assert await settings.get("TURN_PROGRESS_GRACE_S") == 600.0
    assert await _keys(test_db) == {"TURN_PROGRESS_GRACE_S"}


def test_registered_in_registry():
    from migrations.runner import _MIGRATIONS

    names = [name for name, _ in _MIGRATIONS]
    assert "turn_timeout_override_fold" in names
