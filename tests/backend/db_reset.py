"""Test-database naming + the pre-schema reset statement.

Separate module, not conftest: a test that did `from conftest import …` would execute
conftest's module body a SECOND time under a different module name and deadlock on its own
suite flock. Both conftest and the tests import from here instead.
"""
from __future__ import annotations

import re

# The only database names the reset is allowed to touch: test_gw0 … test_gw7, one per
# pytest-xdist worker (conftest's INVARIANT caps the count at 8).
_TEST_DB_RE = re.compile(r"^test_gw\d+$")


def db_name_for_worker(worker_id: str) -> str:
    """Map an xdist worker id (`gw0`, `gw3`, …) to its dedicated database name."""
    return f"test_{worker_id}"


def reset_test_database_sql(db_name: str) -> str:
    """Return the statement that drops `db_name` wholesale, or raise if it is not a test db.

    INVARIANT(data-loss): only a `test_gwN` database may be reset.
    Why: this statement destroys an entire database. The caller passes a name assembled
    from an environment variable, so one bad PYTEST_XDIST_WORKER (or a future rename of the
    naming scheme) must fail loudly instead of dropping a real database.
    """
    if not _TEST_DB_RE.match(db_name or ""):
        raise RuntimeError(
            f"refusing to reset database {db_name!r}: not a test_gwN test database"
        )
    return f"REMOVE DATABASE IF EXISTS {db_name}"
