"""SANDBOX_SSH_KEY_FILE — the bundled-key fallback behind SANDBOX_SSH_KEY_B64.

The private key reaches the backend two ways: env SANDBOX_SSH_KEY_B64 (the
external-sandbox contract prod already runs) or a file named by
SANDBOX_SSH_KEY_FILE (the compose-bundled sandbox's keygen volume, mounted
read-only at the backend). Env always wins, so an external sandbox overrides
the bundled key; a missing file resolves to empty — "tool not served", not a
crash (the same valid-deploy stance as an unset SANDBOX_SSH_HOST).

Config folds the fallback at IMPORT (the instance_secret.resolve precedent
behind SECRET_KEY), so every case runs in a FRESH interpreter via subprocess
(testing.md: a guard over process-START state) — in-process monkeypatching
would re-bind an attribute, not re-run the import-time fold this file pins.
Cases 1/3/4 pin existing behaviour (env precedence, empty-on-missing) so the
fallback cannot silently invert it; case 2 is the new fold.
"""

import base64
import json
import os
import pathlib
import subprocess
import sys

_BACKEND = pathlib.Path("/app")

_CHILD = (
    "import json, config; print(json.dumps({"
    "'key': config.SANDBOX_SSH_KEY_B64, 'enabled': config.SANDBOX_ENABLED}))"
)

# INVARIANT: the two key axes are the ONLY sandbox env the child may differ on.
# Why: SANDBOX_ENABLED folds host AND key at import, so an inherited host value
# (gray sets it, CI ships .env.example's) would decide `enabled` on an axis the
# test never chose.
_AXES = ("SANDBOX_SSH_KEY_B64", "SANDBOX_SSH_KEY_FILE")


def _resolve(**overrides: str | None) -> dict:
    """Import config in a fresh interpreter with the sandbox env axes pinned.

    SANDBOX_SSH_HOST is pinned set (so `enabled` depends only on the key);
    each axis is set to its override when str, else REMOVED — present-but-empty
    must stay testable because `.env.example` ships `SANDBOX_SSH_KEY_B64=`
    uncommented-empty, and that is the shape the file fallback must catch.
    """
    env = dict(os.environ)
    env["SANDBOX_SSH_HOST"] = "sandbox"
    for name in _AXES:
        value = overrides.get(name)
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    out = subprocess.run(
        [sys.executable, "-c", _CHILD], cwd=_BACKEND, env=env,
        capture_output=True, text=True, check=True, timeout=120,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_env_key_wins_over_file(tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\nfile-loses\n")  # gitleaks:allow
    got = _resolve(SANDBOX_SSH_KEY_B64="ZW52LXZhbHVl", SANDBOX_SSH_KEY_FILE=str(key))
    assert got["key"] == "ZW52LXZhbHVl"
    assert got["enabled"] is True


def test_empty_env_plus_file_becomes_base64(tmp_path):
    key = tmp_path / "id_ed25519"
    pem = b"-----BEGIN OPENSSH PRIVATE KEY-----\nbundled\n-----END-----\n"
    key.write_bytes(pem)
    got = _resolve(SANDBOX_SSH_KEY_B64="", SANDBOX_SSH_KEY_FILE=str(key))
    assert got["key"] == base64.b64encode(pem).decode("ascii")
    assert got["enabled"] is True


def test_empty_env_plus_missing_file_is_empty_and_disabled(tmp_path):
    got = _resolve(SANDBOX_SSH_KEY_B64="", SANDBOX_SSH_KEY_FILE=str(tmp_path / "absent"))
    assert got["key"] == ""
    assert got["enabled"] is False


def test_no_env_no_path_is_empty():
    got = _resolve()
    assert got["key"] == ""
    assert got["enabled"] is False
