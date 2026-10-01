"""Wiring between Lore's own components is not configuration — constants, not settings.

The harness line's address/secret and the converter URL (step 1), the storage
root, the Redis address and the session-key env leg (step 2), the database
address/user/namespace/database (step 4) — plan component-wiring-not-settings.
A value a person could set was a value the two sides could disagree on; the
addresses are compose service names, the secrets are files secrets-init
generates per install.

TWO kinds of name here:
- MUST-NOT-MOVE axes — env names the removed settings used to read. Setting
  any must not change what config resolves.
- TEST SEAMS (`STORAGE_PATH`, `REDIS_URL`, `SURREAL_NS`, `SURREAL_DB`) — the
  env read is the seam itself (conftest isolates each xdist worker on its own
  storage subtree, Redis db 15-N, and points the suite at the lore_test
  namespace / per-worker test_gwN databases); the constant is only the
  DEFAULT. Removing them must resolve the constant; setting them must move
  config (a bare constant would point this suite at the LIVE db 0 / lore/main
  and wipe it).

Config binds at IMPORT, so every resolution case runs in a FRESH interpreter
(testing.md: a guard over process-START state).
"""

import json
import os
import pathlib
import subprocess
import sys

import config

_BACKEND = pathlib.Path("/app")
_CHILD = (
    "import json, config, logging_conf; print(json.dumps(["
    "config.HARNESS_DRIVER_URL, config.HARNESS_DRIVER_SECRET, "
    "config.CONVERTER_URL, "
    "str(config.STORAGE_PATH), config.REDIS_URL, config.SECRET_KEY, "
    "logging_conf.LOG_DIR, "
    "config.SURREAL_URL, config.SURREAL_USER, config.SURREAL_NS, config.SURREAL_DB, "
    "hasattr(config, 'TOOL_API_INTERNAL_URL'), hasattr(config, 'SURREAL_PASS')]))"
)

#: Env names the removed settings used to read — setting any must not move
#: the constants.
_MUST_NOT_MOVE = ("HARNESS_DRIVER_URL", "HARNESS_DRIVER_SECRET",
                  "HARNESS_DRIVER_SECRET_FILE", "CONVERTER_URL",
                  "LORE_SECRET_KEY", "TOOL_API_INTERNAL_URL", "LOG_DIR",
                  "SURREAL_URL", "SURREAL_USER", "SURREAL_PASS",
                  "SURREAL_PASS_FILE")

#: The seam envs: the child is run WITHOUT them to prove the constant default
#: (conftest sets all four in this process — a plain copy would inherit them).
_SEAMS = ("STORAGE_PATH", "REDIS_URL", "SURREAL_NS", "SURREAL_DB")


def _resolve(**overrides: str | None) -> list:
    """Import config in a fresh interpreter under the given env shape.

    Every _MUST_NOT_MOVE and _SEAM name is dropped first, then the overrides
    re-set exactly the ones the caller names — so an unset override means
    "absent", never "inherited from this process's conftest-shaped env".
    """
    env = dict(os.environ)
    for name in _MUST_NOT_MOVE + _SEAMS:
        env.pop(name, None)
    env.update({k: v for k, v in overrides.items() if v is not None})
    out = subprocess.run(
        [sys.executable, "-c", _CHILD], cwd=_BACKEND, env=env,
        capture_output=True, text=True, check=True, timeout=120,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def _secret_file_value() -> str:
    """What a fresh process reads — derived from the same file config reads,
    never a literal (the container may or may not mount the secrets volume)."""
    f = pathlib.Path("/secrets/harness/driver_secret")
    return f.read_text().strip() if f.is_file() else ""


def _session_key_file(storage_path: str) -> pathlib.Path:
    return pathlib.Path(storage_path) / ".lore" / "secret_key"


def test_env_cannot_move_the_wiring_constants():
    assert _resolve(
        HARNESS_DRIVER_URL="http://evil:1",
        HARNESS_DRIVER_SECRET="from-env",
        HARNESS_DRIVER_SECRET_FILE="/nonexistent",
        CONVERTER_URL="http://evil:2",
        LORE_SECRET_KEY="typed-secret",
        TOOL_API_INTERNAL_URL="http://evil:3",
        LOG_DIR="/evil-logs",
        SURREAL_URL="ws://evil:4/rpc",
        SURREAL_USER="evil-user",
        SURREAL_PASS="evil-pass",
        SURREAL_PASS_FILE="/evil/pass",
    ) == [
        "http://harness:8090", _secret_file_value(), "http://converter:8002",
        "/storage", "redis://redis:6379/0",
        # The session key comes from the generated file under the (constant)
        # storage root — the fresh import above just created it.
        _session_key_file("/storage").read_text().strip(),
        "/logs",
        "ws://surreal:8000/rpc", "root", "lore", "main",
        False,  # TOOL_API_INTERNAL_URL: not even a config attr anymore
        False,  # SURREAL_PASS: the password is a file config names, not a value
    ]


def test_no_env_keeps_the_file_contract():
    (url, secret, converter, storage, redis, session_key,
     log_dir, db_url, db_user, db_ns, db_db, has_tool_api, has_db_pass) = _resolve()
    assert url == "http://harness:8090"
    assert converter == "http://converter:8002"
    assert storage == "/storage"
    assert redis == "redis://redis:6379/0"
    assert log_dir == "/logs"
    assert db_url == "ws://surreal:8000/rpc"
    assert db_user == "root"
    assert db_ns == "lore"
    assert db_db == "main"
    assert has_tool_api is False
    assert has_db_pass is False
    assert secret == _secret_file_value()
    # The session key exists only as the persisted per-install file.
    key_file = _session_key_file(storage)
    assert key_file.is_file()
    assert session_key == key_file.read_text().strip()
    assert session_key  # never empty — an empty key would sign forgeable JWTs


def test_seam_envs_still_move_their_values():
    """STORAGE_PATH / REDIS_URL / SURREAL_NS / SURREAL_DB keep their env read
    — the conftest seams.

    A bare constant here would point the suite's workers at the live db 0, the
    shared storage root and the LIVE lore/main database: the seams are
    load-bearing, not leftover config.
    """
    values = _resolve(
        STORAGE_PATH="/tmp/wiring-seam-storage",
        REDIS_URL="redis://seam:6379/7",
        SURREAL_NS="lore_seam",
        SURREAL_DB="seam_db",
    )
    storage, redis, session_key = values[3], values[4], values[5]
    db_ns, db_db = values[9], values[10]
    assert storage == "/tmp/wiring-seam-storage"
    assert redis == "redis://seam:6379/7"
    assert db_ns == "lore_seam"
    assert db_db == "seam_db"
    # The session key follows the storage root it is generated under.
    assert session_key == _session_key_file(storage).read_text().strip()


def test_conftest_isolation_reaches_config():
    """This worker's config IS the conftest-shaped one (the seam end-to-end):
    Redis db 15-N (never the live db 0) and the per-worker storage subtree."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    idx = int(worker.removeprefix("gw"))
    assert config.REDIS_URL == f"redis://redis:6379/{15 - idx}"
    assert config.STORAGE_PATH == pathlib.Path(f"/tmp/lore_test_storage/w{idx}")
