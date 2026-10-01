"""web_search_off_drop: the persisted `off` is deleted, a live choice stays.

`off` stopped being a WEB_SEARCH_PROVIDER choice; override rows are read
without validation (settings.load_overrides), so a stored `off` would reach
the turn-payload builder and break every turn. The migration deletes exactly
that row so the default (deepseek) applies.
"""

import json

import pytest

from migrations.migrate_web_search_off_drop import _migrate_web_search_off_drop


@pytest.fixture(autouse=True)
def _pinned_default(monkeypatch):
    """The default the deleted row falls back to, pinned: settings.get prefers
    an env-present key and config holds the import-time value, so either one
    on the runner would answer `deepseek` (or not) by coincidence."""
    import config

    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "deepseek")


async def _put_override(test_db, value: str) -> None:
    await test_db.query(
        "UPSERT type::record('instance_settings', $key) SET "
        "key = $key, value = $value, updated_by = $by, updated_at = time::now()",
        {"key": "WEB_SEARCH_PROVIDER", "value": json.dumps(value), "by": "migration-test"},
    )


@pytest.mark.asyncio
async def test_off_override_is_deleted_and_the_default_applies(test_db):
    import settings

    settings.drop_cache()
    await _put_override(test_db, "off")
    await _migrate_web_search_off_drop(test_db)
    settings.drop_cache()
    assert await settings.get("WEB_SEARCH_PROVIDER") == "deepseek"


@pytest.mark.asyncio
async def test_a_live_choice_is_untouched(test_db):
    import settings

    settings.drop_cache()
    await _put_override(test_db, "brave")
    await _migrate_web_search_off_drop(test_db)
    settings.drop_cache()
    assert await settings.get("WEB_SEARCH_PROVIDER") == "brave"


@pytest.mark.asyncio
async def test_no_override_and_a_rerun_are_noops(test_db):
    await _migrate_web_search_off_drop(test_db)
    await _put_override(test_db, "off")
    await _migrate_web_search_off_drop(test_db)
    await _migrate_web_search_off_drop(test_db)
    rows = await test_db.query(
        "SELECT key, value FROM instance_settings WHERE key = 'WEB_SEARCH_PROVIDER'")
    assert rows == []


def test_registered_in_registry():
    from migrations.runner import _MIGRATIONS

    names = [name for name, _ in _MIGRATIONS]
    assert "web_search_off_drop" in names
