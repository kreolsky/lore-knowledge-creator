"""The instance JWT secret: an explicit env value wins, otherwise one random key
is created on first boot and persisted under the storage root."""

import threading

import pytest
from instance_secret import resolve
from settings_registry import ConfigError


def test_env_value_wins_and_touches_no_file(tmp_path):
    assert resolve("from-env", tmp_path) == "from-env"
    assert not (tmp_path / ".lore").exists()


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
