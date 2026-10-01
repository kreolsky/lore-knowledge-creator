"""Generate an install's internal secrets before the services that share them boot.

Run by the compose `secrets-init` one-shot on every `up` (from /app,
writing under /secrets):

    python -m scripts.init_secrets

`surreal/pass` and `harness/driver_secret` are random, created once and never
rewritten. `surreal/init.surql` is re-rendered every run and SurrealDB imports
it at start, so the root password always equals what the backend signs in
with. The root user is the constant `root` (config.SURREAL_USER — every
install's name; there is no env leg: wiring between Lore's own components is
not configuration, plan component-wiring-not-settings).
"""

from pathlib import Path

from instance_secret import ensure_key


def _surql_string(value: str) -> str:
    """A single-quoted SurrealQL string literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def init_surreal(root: Path) -> None:
    """Ensure the database password under `root` and render the SurrealDB import file.

    # INVARIANT(security): the password exists ONLY as the generated (or
    # operator-seeded) file — env is ignored, an existing file is kept.
    # Why: a second password source is a way for the file and the running
    # database to disagree, which locks the backend out of its own database;
    # OVERWRITE re-asserts the FILE's password on every start, so the pair
    # cannot drift.
    """
    password = ensure_key(root / "surreal" / "pass")
    init = root / "surreal" / "init.surql"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(f"DEFINE USER OVERWRITE root ON ROOT PASSWORD {_surql_string(password)} ROLES OWNER;\n")
    init.chmod(0o600)


def init_secrets(root: Path) -> None:
    """Ensure every internal secret under `root`: the database pair and the
    driver secret the backend and the harness share."""
    init_surreal(root)
    ensure_key(root / "harness" / "driver_secret")


if __name__ == "__main__":
    init_secrets(Path("/secrets"))
