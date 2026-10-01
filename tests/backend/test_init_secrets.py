"""The one-shot that generates an install's internal secrets.

The database password and the harness driver secret are written once (random,
0600, never rewritten); the SurrealDB import file is re-rendered on every run,
so the database password always matches what the backend will sign in with.
The root user is the constant `root` (db/pool.py signs in with it) and env
SURREAL_PASS is ignored — wiring between Lore's own components is not
configuration (plan component-wiring-not-settings step 4).
"""

import os
import stat

from scripts.init_secrets import init_secrets


def _run(root):
    init_secrets(root)


def _init(root) -> str:
    return (root / "surreal" / "init.surql").read_text()


def test_first_run_creates_private_random_secrets(tmp_path):
    _run(tmp_path)
    for rel in ("harness/driver_secret", "surreal/pass"):
        path = tmp_path / rel
        value = path.read_text().strip()
        assert len(value) == 64 and int(value, 16) >= 0
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    password = (tmp_path / "surreal" / "pass").read_text().strip()
    assert _init(tmp_path) == f"DEFINE USER OVERWRITE root ON ROOT PASSWORD '{password}' ROLES OWNER;\n"


def test_later_runs_keep_the_secrets(tmp_path):
    _run(tmp_path)
    first = {p: (tmp_path / p).read_text() for p in ("harness/driver_secret", "surreal/pass", "surreal/init.surql")}
    _run(tmp_path)
    assert {p: (tmp_path / p).read_text() for p in first} == first


def test_env_surreal_pass_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("SURREAL_PASS", "chosen")
    _run(tmp_path)
    password = (tmp_path / "surreal" / "pass").read_text().strip()
    assert password != "chosen"
    assert f"PASSWORD '{password}'" in _init(tmp_path)


def test_an_existing_file_survives_even_with_env_set(tmp_path, monkeypatch):
    (tmp_path / "surreal").mkdir()
    (tmp_path / "surreal" / "pass").write_text("kept\n")
    monkeypatch.setenv("SURREAL_PASS", "chosen")
    _run(tmp_path)
    assert (tmp_path / "surreal" / "pass").read_text() == "kept\n"
    assert "PASSWORD 'kept'" in _init(tmp_path)


def test_quotes_and_backslashes_in_the_password_are_escaped(tmp_path):
    # The file may hold an operator-seeded password (the prod migration), not
    # only ensure_key's hex — the SurrealQL literal must escape it.
    (tmp_path / "surreal").mkdir()
    (tmp_path / "surreal" / "pass").write_text("a'b\\c\n")
    _run(tmp_path)
    assert "PASSWORD 'a\\'b\\\\c'" in _init(tmp_path)
