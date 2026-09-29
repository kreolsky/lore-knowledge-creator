"""The instance JWT secret: an explicit env value wins, otherwise one random key
is created on first boot and persisted under the storage root."""

import os
import stat
import threading

import pytest
from instance_secret import resolve
from settings_registry import ConfigError


def test_env_value_wins_and_touches_no_file(tmp_path):
    assert resolve("from-env", tmp_path) == "from-env"
    assert not (tmp_path / ".lore").exists()


def test_first_boot_creates_a_private_random_key(tmp_path):
    key = resolve("", tmp_path)
    path = tmp_path / ".lore" / "secret_key"
    assert len(key) == 64 and int(key, 16) >= 0
    assert path.read_text().strip() == key
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_later_boots_reuse_the_persisted_key(tmp_path):
    assert resolve("", tmp_path) == resolve("", tmp_path)


def test_two_instances_get_different_keys(tmp_path):
    assert resolve("", tmp_path / "a") != resolve("", tmp_path / "b")


def test_concurrent_first_boots_agree_on_one_key(tmp_path):
    keys: list[str] = []
    barrier = threading.Barrier(8)

    def boot():
        barrier.wait()
        keys.append(resolve("", tmp_path))

    threads = [threading.Thread(target=boot) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(keys)) == 1
    assert (tmp_path / ".lore" / "secret_key").read_text().strip() == keys[0]


def test_an_empty_persisted_file_is_refused_not_used(tmp_path):
    (tmp_path / ".lore").mkdir()
    (tmp_path / ".lore" / "secret_key").write_text("\n")
    with pytest.raises(ConfigError):
        resolve("", tmp_path)
