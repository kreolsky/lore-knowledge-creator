"""The database password the backend signs in with: only the file secrets-init
generated (config.SURREAL_PASS_FILE — a wiring constant, plan
component-wiring-not-settings step 4). No env leg: SURREAL_PASS /
SURREAL_PASS_FILE env values are ignored, and a missing or empty file is a
ConfigError, never a silent empty password."""


import pytest
from settings_registry import ConfigError

import config


async def test_a_missing_file_fails_startup_at_once_with_the_upgrade_hint(tmp_path, monkeypatch, caplog):
    """A missing password file is the install's wiring (a pre-v0.21 compose has
    no secrets-init), not a database still booting: the startup loop raises it
    on the first attempt instead of logging "not ready" for a minute."""
    import db.pool

    monkeypatch.setattr(config, "SURREAL_PASS_FILE", tmp_path / "absent")
    monkeypatch.setattr(db.pool, "_STARTUP_MAX_WAIT", 1.0)
    monkeypatch.setattr(db.pool.DBPool, "_instance", None)
    pool = db.pool.DBPool()

    with pytest.raises(ConfigError, match="docker-compose.yml older than v0.21.0"):
        await pool.get_db()
    assert not [r for r in caplog.records if "not ready" in r.getMessage()]
