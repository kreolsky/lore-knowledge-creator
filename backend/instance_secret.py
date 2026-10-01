"""Per-instance random secrets: the JWT signing key and the keys secrets-init persists.

A self-hosted install starts with no configuration at all, so the key cannot be
something the operator types. It is created once, on first boot, under the
storage root and reused by every process (backend and workers) afterwards.
"""

import os
import secrets
from pathlib import Path

from settings_registry import ConfigError

_KEY_FILE = Path(".lore") / "secret_key"


def resolve(env_value: str, storage_path: Path) -> str:
    """Return `env_value` when set, otherwise the persisted instance key.

    Raises ConfigError when the persisted file exists but holds no key — an
    empty key would sign sessions anyone can forge.
    """
    # INVARIANT(security): the key is per-instance random, never a shipped default.
    # Why: a default baked into the image or compose file is the same on every
    # install, so anyone could sign a session cookie for any other instance.
    if env_value:
        return env_value
    try:
        return ensure_key(storage_path / _KEY_FILE)
    except ConfigError as exc:
        raise ConfigError(f"{exc} (logs everyone out)") from exc


def ensure_key(path: Path) -> str:
    """Return the random key persisted at `path`, creating it on first call.

    Raises ConfigError when the file exists but holds no key.
    """
    if not path.exists():
        _create(path)
    key = path.read_text().strip()
    if not key:
        raise ConfigError(f"{path} is empty — delete it to generate a new key")
    return key


def _create(path: Path) -> None:
    """Write a new key atomically; a concurrent first boot that wins keeps its key.

    WHY temp + link: backend and both workers import config at the same time on a
    fresh install. `os.link` fails if the name exists, so exactly one key is ever
    published, and a reader never sees a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_hex(32) + "\n")
    try:
        os.link(tmp, path)
    except FileExistsError:
        pass
    finally:
        tmp.unlink()
