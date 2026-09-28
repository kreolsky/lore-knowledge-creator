"""Guard tests for the test-database reset that runs before the schema is applied.

The reset exists because a leftover row can outlive a run: run #898 hit a `chat_sessions`
row with `created_at = NONE` that had sat in database `test_gw5` since an earlier run, and
schema.surql's data backfill (`UPDATE chat_sessions SET mode = 'chat' WHERE mode = NONE`)
re-coerced it and failed — 581 test setups died at setup. These tests pin the guard that
keeps the reset pointed at a test database and nothing else.
"""
import pytest
from db_reset import db_name_for_worker, reset_test_database_sql


def test_reset_sql_targets_the_named_database():
    assert reset_test_database_sql("test_gw3") == "REMOVE DATABASE IF EXISTS test_gw3"


@pytest.mark.parametrize("worker_id", [f"gw{i}" for i in range(8)])
def test_every_worker_database_is_resettable(worker_id):
    # Derived from the naming helper conftest itself uses, over the full worker range its
    # INVARIANT allows — so a rename of the scheme fails here instead of silently
    # disarming the reset.
    name = db_name_for_worker(worker_id)
    assert reset_test_database_sql(name).endswith(name)


@pytest.mark.parametrize("name", ["lore", "production", "test", "", "gw0", "TEST_gw0", "test_gwN"])
def test_reset_sql_refuses_any_non_test_database(name):
    # The principal that MUST be refused: this statement destroys a whole database, so a
    # name outside the test_gwN scheme has to raise rather than execute.
    with pytest.raises(RuntimeError, match="refusing"):
        reset_test_database_sql(name)
