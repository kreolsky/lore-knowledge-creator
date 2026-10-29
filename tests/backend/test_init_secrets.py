"""The one-shot that generates an install's internal secrets.

The database password and the harness driver secret are written once (random,
0600, never rewritten); the SurrealDB import file is re-rendered on every run,
so the database password always matches what the backend will sign in with.
The root user is the constant `root` (db/pool.py signs in with it) and env
SURREAL_PASS is ignored — wiring between Lore's own components is not
configuration (plan component-wiring-not-settings step 4).
"""


from scripts.init_secrets import init_secrets


def _run(root):
    init_secrets(root)


def _init(root) -> str:
    return (root / "surreal" / "init.surql").read_text()


def test_quotes_and_backslashes_in_the_password_are_escaped(tmp_path):
    # The file may hold an operator-seeded password (the prod migration), not
    # only ensure_key's hex — the SurrealQL literal must escape it.
    (tmp_path / "surreal").mkdir()
    (tmp_path / "surreal" / "pass").write_text("a'b\\c\n")
    _run(tmp_path)
    assert "PASSWORD 'a\\'b\\\\c'" in _init(tmp_path)
