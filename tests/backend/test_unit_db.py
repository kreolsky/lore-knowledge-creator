"""Unit tests for pure functions in db.py, plus integration tests for fetch_many."""

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from db import (
    _fmt_datetime,
    extract_id,
    fetch_many,
    record_refs,
    serialize_record,
    validate_record_id,
)


class TestExtractId:
    def test_none(self):
        assert extract_id(None) is None

    def test_table_colon_uuid(self):
        assert extract_id("users:abc-123") == "abc-123"

    def test_table_colon_backtick(self):
        assert extract_id("users:`abc-123`") == "abc-123"

    def test_table_colon_angle(self):
        assert extract_id("users:⟨abc-123⟩") == "abc-123"

    def test_recordid_repr(self):
        s = "RecordID(table_name=users, record_id=`abc-123`)"
        assert extract_id(s) == "abc-123"

    def test_plain_string(self):
        assert extract_id("abc-123") == "abc-123"

    def test_plain_with_angle(self):
        assert extract_id("⟨abc-123⟩") == "abc-123"


class TestValidateRecordId:
    def test_valid_uuid(self):
        assert validate_record_id("abc-123-def") == "abc-123-def"

    def test_valid_alphanumeric(self):
        assert validate_record_id("user_001") == "user_001"

    def test_rejects_semicolon_injection(self):
        with pytest.raises(ValueError):
            validate_record_id("abc; DROP TABLE users")

    def test_rejects_quotes(self):
        with pytest.raises(ValueError):
            validate_record_id("abc'123")

    def test_rejects_spaces(self):
        with pytest.raises(ValueError):
            validate_record_id("abc 123")

    def test_rejects_angle_brackets(self):
        with pytest.raises(ValueError):
            validate_record_id("<script>")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError):
            validate_record_id("")


class TestRecordRefs:
    def test_binds_ids_as_params(self):
        """Ids ride params; no id bytes reach the query-text fragment."""
        refs, params = record_refs("documents", ["aaa-1", "bbb_2"])
        assert refs == (
            "type::record('documents', $id0), type::record('documents', $id1)"
        )
        assert params == {"id0": "aaa-1", "id1": "bbb_2"}
        assert "aaa-1" not in refs and "bbb_2" not in refs

    def test_rejects_bad_table(self):
        with pytest.raises(ValueError):
            record_refs("documents; DROP TABLE users", ["aaa"])

    def test_rejects_bad_table_even_with_no_ids(self):
        with pytest.raises(ValueError):
            record_refs("users'; --", [])

    def test_empty_ids(self):
        assert record_refs("documents", []) == ("", {})

    def test_rejects_bad_id(self):
        with pytest.raises(ValueError):
            record_refs("documents", ["aaa", "bbb'--"])

    def test_prefix_avoids_param_collision(self):
        refs, params = record_refs("users", ["u-1"], prefix="owner")
        assert refs == "type::record('users', $owner0)"
        assert params == {"owner0": "u-1"}


class TestFmtDatetime:
    def test_none(self):
        assert _fmt_datetime(None) is None

    def test_datetime_object(self):
        dt = datetime(2025, 3, 15, 14, 30, tzinfo=timezone.utc)
        assert _fmt_datetime(dt) == "2025-03-15 14:30"

    def test_iso_string_with_z(self):
        assert _fmt_datetime("2025-03-15T14:30:00Z") == "2025-03-15 14:30"

    def test_iso_string_with_offset(self):
        assert _fmt_datetime("2025-03-15T14:30:00+00:00") == "2025-03-15 14:30"

    def test_invalid_string(self):
        assert _fmt_datetime("not-a-date") is None

    def test_integer(self):
        assert _fmt_datetime(12345) is None


class TestSerializeRecord:
    def test_renames_id(self):
        record = {"id": "users:abc-123", "name": "Alice"}
        result = serialize_record(record, "user_id")
        assert result["user_id"] == "abc-123"
        assert "id" not in result

    def test_formats_datetime_fields(self):
        dt = datetime(2025, 3, 15, 14, 30, tzinfo=timezone.utc)
        record = {"id": "x:1", "created_at": dt}
        result = serialize_record(record, "uid")
        assert result["created_at_fmt"] == "2025-03-15 14:30"

    def test_skips_deleted_at_fmt(self):
        dt = datetime(2025, 3, 15, 14, 30, tzinfo=timezone.utc)
        record = {"id": "x:1", "deleted_at": dt}
        result = serialize_record(record, "uid")
        assert "deleted_at_fmt" not in result

    def test_preserves_regular_fields(self):
        record = {"id": "x:1", "name": "foo", "count": 42}
        result = serialize_record(record, "uid")
        assert result["name"] == "foo"
        assert result["count"] == 42

    def test_datetime_fields_are_json_safe_iso_strings(self):
        # Regression: SurrealDB 2.0+ returns datetime columns as Python datetime
        # objects; event-bus payloads pass through json.dumps and crash on them.
        import json

        dt = datetime(2025, 3, 15, 14, 30, tzinfo=timezone.utc)
        record = {"id": "document_history:1", "created_at": dt, "action": "created"}
        result = serialize_record(record, "id")
        assert isinstance(result["created_at"], str)
        assert result["created_at"] == dt.isoformat()
        # The whole payload must survive json.dumps (the broadcast boundary).
        json.dumps(result)


# ─── fetch_many integration tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_many_returns_matching_records(test_db):
    """fetch_many returns only the requested records by ID."""
    from db import create_record
    ids = [str(uuid4()) for _ in range(3)]
    for uid in ids:
        await create_record("users", uid, {"name": f"user-{uid[:8]}", "role": "user"})
    result = await fetch_many("users", ids[:2])
    assert set(result.keys()) == set(ids[:2])


@pytest.mark.asyncio
async def test_fetch_many_excludes_soft_deleted(test_db):
    """fetch_many skips records where deleted_at is set."""
    from db import create_record, soft_delete
    uid = str(uuid4())
    await create_record("users", uid, {"name": "to-delete", "role": "user"})
    await soft_delete("users", uid)
    result = await fetch_many("users", [uid])
    assert uid not in result


@pytest.mark.asyncio
async def test_fetch_many_empty_input(test_db):
    """fetch_many with empty list returns empty dict without DB call."""
    result = await fetch_many("users", [])
    assert result == {}


# ─── DBPool unit tests ──────────────────────────────────────────────────────


class TestDBPool:
    def _make_fresh_pool(self):
        from db import DBPool
        pool = DBPool.__new__(DBPool)
        pool._db = None
        pool._db_lock = asyncio.Lock()
        pool._last_check_ts = 0.0
        pool._startup_complete = False
        return pool

    def test_get_returns_singleton_instance(self):
        from db import DBPool
        p1 = DBPool()
        p2 = DBPool()
        assert p1 is p2

    def test_initial_state(self):
        pool = self._make_fresh_pool()
        assert pool._db is None
        assert pool._startup_complete is False

    @pytest.mark.asyncio
    async def test_get_db_reuses_cached_connection(self):
        pool = self._make_fresh_pool()
        mock_conn = AsyncMock()
        mock_conn.query.return_value = [1]
        pool._db = mock_conn
        pool._last_check_ts = time.monotonic()
        pool._startup_complete = True
        db1 = await pool.get_db()
        db2 = await pool.get_db()
        assert db1 is mock_conn
        assert db2 is mock_conn

    @pytest.mark.asyncio
    async def test_reset_db_clears_connection(self):
        pool = self._make_fresh_pool()
        mock_conn = AsyncMock()
        pool._db = mock_conn
        pool._last_check_ts = time.monotonic()
        await pool.reset_db()
        assert pool._db is None

    def test_mark_startup_complete(self):
        pool = self._make_fresh_pool()
        assert pool._startup_complete is False
        pool.mark_startup_complete()
        assert pool._startup_complete is True


# ─── run_in_transaction() integration tests ─────────────────────────────────
#
# These hit the real test DB (not mocks) because the bug being fixed is purely a
# DB-side behavior: separate BEGIN/op/COMMIT RPCs in surrealdb-py 1.0.4 do NOT
# share a transaction scope, so a mock that only records "BEGIN/COMMIT were
# called" proves nothing about rollback. We assert that a mid-block failure
# leaves NO rows behind.


def _create_user(var: str, role: str = "user") -> str:
    """Build a CREATE statement for the users table using a bind var for the id."""
    return f"CREATE type::record('users', ${var}) SET name = 'x', role = '{role}', user_facts = ''"


async def _user_exists(db, uid: str) -> bool:
    rows = await db.query(
        "SELECT meta::id(id) AS id FROM users WHERE meta::id(id) = $id", {"id": uid}
    )
    return bool(rows)


class TestRunInTransaction:
    @pytest.mark.asyncio
    async def test_commits_on_success(self, test_db):
        from db import run_in_transaction
        a, b = str(uuid4()), str(uuid4())
        await run_in_transaction(
            test_db, [_create_user("a"), _create_user("b")], {"a": a, "b": b}
        )
        assert await _user_exists(test_db, a)
        assert await _user_exists(test_db, b)

    @pytest.mark.asyncio
    async def test_rolls_back_on_error(self, test_db):
        """A valid write followed by a failing one (role ASSERT) rolls back BOTH.

        This is the exact regression the old context-manager helper could not
        catch: it committed the first write because the second 'failure' never
        raised a Python exception.
        """
        from db import run_in_transaction
        a, b = str(uuid4()), str(uuid4())
        with pytest.raises(RuntimeError):
            await run_in_transaction(
                test_db,
                [_create_user("a"), _create_user("b", role="BOGUS")],
                {"a": a, "b": b},
            )
        assert not await _user_exists(test_db, a)
        assert not await _user_exists(test_db, b)

    @pytest.mark.asyncio
    async def test_raises_with_real_cause(self, test_db):
        """The raised error carries the underlying DB cause, not the generic
        'query was not executed due to a failed transaction' noise."""
        from db import run_in_transaction
        a = str(uuid4())
        with pytest.raises(RuntimeError, match="role"):
            await run_in_transaction(test_db, [_create_user("a", role="BOGUS")], {"a": a})

    @pytest.mark.asyncio
    async def test_throw_rolls_back_and_surfaces_sentinel(self, test_db):
        """THROW aborts the whole transaction; the sentinel is recoverable from
        the raised error (this is how embeddings.py detects a CAS skip)."""
        from db import run_in_transaction
        a = str(uuid4())
        with pytest.raises(RuntimeError, match="version_changed"):
            await run_in_transaction(
                test_db, [_create_user("a"), "THROW 'version_changed'"], {"a": a}
            )
        assert not await _user_exists(test_db, a)


# ─── ConfigError unit tests ─────────────────────────────────────────────────


class TestConfigError:
    def test_config_error_is_runtime_error(self):
        from config import ConfigError
        assert issubclass(ConfigError, RuntimeError)

    def test_require_env_raises_config_error(self):
        from config import _require_env
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError):
                _require_env("NONEXISTENT_VAR_12345")
