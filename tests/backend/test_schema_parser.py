"""Regression tests for split_schema_statements — ensures comment lines do not
cause DEFINE statements to be silently dropped."""

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _mock_surrealdb():
    if "surrealdb" not in sys.modules:
        mod = types.ModuleType("surrealdb")
        mod.AsyncSurreal = MagicMock()
        sys.modules["surrealdb"] = mod


@pytest.fixture()
def db_module(_mock_surrealdb):
    from surrealdb.connections.async_ws import AsyncWsSurrealConnection as _Conn

    # INVARIANT: restore sys.modules["db"] to the conftest-patched module on teardown.
    # Why: reloading db into a fresh module leaves an UNpatched get_db (+ fresh _pool)
    # in sys.modules; later tests doing lazy `from db import ...` then bypass the test
    # connection. Same leak as test_db_recv_patch (2026-05-30).
    saved_db = sys.modules.get("db")
    # Snapshot the real class's _recv_task BEFORE reload. importlib.reload(db) re-runs
    # the module body and re-binds AsyncWsSurrealConnection._recv_task to a NEW
    # _patched_recv_task object (the reloaded module's). The sys.modules["db"] restore
    # below does NOT touch the class attribute — so the stale (reloaded) function
    # leaks and is not identity-equal to the canonical db._patched_recv_task, which
    # makes test_sdk_contract's contract #3 identity check fail when both files land
    # on the same -n 4 gateway worker. Restore the pre-reload patch object here so the
    # class surface stays canonical. (Same fix as test_db_recv_patch.db_module — 1be539f
    # covered that site but not this one.)
    saved_recv_task = getattr(_Conn, "_recv_task", None)
    if "db" in sys.modules:
        del sys.modules["db"]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import db
    importlib.reload(db)
    yield db
    if saved_recv_task is not None:
        _Conn._recv_task = saved_recv_task
    if saved_db is not None:
        sys.modules["db"] = saved_db
    else:
        sys.modules.pop("db", None)


def test_comment_before_define_preserves_both(db_module):
    chunk = "DEFINE FIELD a ON foo TYPE string; -- section\nDEFINE FIELD b ON foo TYPE int;"
    stmts = db_module.split_schema_statements(chunk)
    texts = " ".join(stmts)
    assert "DEFINE FIELD a" in texts
    assert "DEFINE FIELD b" in texts


def test_pure_comment_line_not_in_output(db_module):
    schema = "-- ─── Section ───"
    stmts = db_module.split_schema_statements(schema)
    assert stmts == []


def test_empty_lines_dont_break_parser(db_module):
    schema = "DEFINE TABLE foo SCHEMAFULL;\n\n\nDEFINE FIELD a ON foo TYPE string;"
    stmts = db_module.split_schema_statements(schema)
    assert len(stmts) == 2


def test_comment_inside_chunk_stripped(db_module):
    schema = "-- header\nDEFINE FIELD x ON bar TYPE string;"
    stmts = db_module.split_schema_statements(schema)
    assert len(stmts) == 1
    assert "--" not in stmts[0]


def test_multiline_chunk_with_leading_comment(db_module):
    schema = (
        "-- sources field for messages table\n"
        "DEFINE FIELD sources ON messages TYPE array DEFAULT [];"
    )
    stmts = db_module.split_schema_statements(schema)
    assert len(stmts) == 1
    assert "DEFINE FIELD sources" in stmts[0]
    assert "--" not in stmts[0]
