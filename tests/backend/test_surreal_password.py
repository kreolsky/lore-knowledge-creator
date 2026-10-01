"""The database password the backend signs in with: only the file secrets-init
generated (config.SURREAL_PASS_FILE — a wiring constant, plan
component-wiring-not-settings step 4). No env leg: SURREAL_PASS /
SURREAL_PASS_FILE env values are ignored, and a missing or empty file is a
ConfigError, never a silent empty password."""

import os

import pytest
from settings_registry import ConfigError

import config
from db.pool import surreal_password


def test_the_password_comes_from_the_file_config_names(tmp_path, monkeypatch):
    (tmp_path / "pass").write_text("from-file\n")
    monkeypatch.setattr(config, "SURREAL_PASS_FILE", tmp_path / "pass")
    assert surreal_password() == "from-file"


def test_env_legs_are_ignored(tmp_path, monkeypatch):
    (tmp_path / "pass").write_text("from-file\n")
    monkeypatch.setattr(config, "SURREAL_PASS_FILE", tmp_path / "pass")
    monkeypatch.setenv("SURREAL_PASS", "from-env")
    monkeypatch.setenv("SURREAL_PASS_FILE", "/nonexistent")
    assert surreal_password() == "from-file"


def test_a_missing_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SURREAL_PASS_FILE", tmp_path / "absent")
    with pytest.raises(ConfigError):
        surreal_password()


def test_an_empty_file_is_refused(tmp_path, monkeypatch):
    (tmp_path / "pass").write_text("\n")
    monkeypatch.setattr(config, "SURREAL_PASS_FILE", tmp_path / "pass")
    with pytest.raises(ConfigError):
        surreal_password()


async def test_pool_signs_in_with_the_file_password_and_the_seam_namespace(tmp_path, monkeypatch):
    """_connect reads the config constants at call time: the constant address
    and user, the FILE password, and the conftest seam ns/db — lore_test and
    this worker's test_gwN, never the live lore/main."""
    from db_reset import db_name_for_worker

    import db.pool

    (tmp_path / "pass").write_text("file-pass\n")
    monkeypatch.setattr(config, "SURREAL_PASS_FILE", tmp_path / "pass")
    seen: dict = {}

    class _FakeSurreal:
        def __init__(self, url):
            seen["url"] = url

        async def signin(self, creds):
            seen["creds"] = creds

        async def use(self, ns, db):
            seen["nsdb"] = (ns, db)

        async def close(self):
            seen["closed"] = True

    monkeypatch.setattr(db.pool, "AsyncSurreal", _FakeSurreal)
    await db.pool._connect()
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    assert seen["url"] == "ws://surreal:8000/rpc"
    assert seen["creds"] == {"username": "root", "password": "file-pass"}
    assert seen["nsdb"] == ("lore_test", db_name_for_worker(worker))
