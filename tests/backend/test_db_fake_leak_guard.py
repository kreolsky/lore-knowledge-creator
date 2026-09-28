"""The conftest guard that unbinds test fakes captured by `from db import X`.

The class it closes: a production module first imported INSIDE a monkeypatched
window binds the fake permanently (monkeypatch restores `db`, not the consumer's
own name), and answers every later test in the process. See `db_leak_guard`.
"""

import routes.tool_api.structure as structure
from db_leak_guard import db_symbols, restore_leaked_db_fakes

import db


async def _fake_fetch_one(_table, _record):
    """A fake defined in a TEST module — exactly what leaks."""
    return {"project_id": "p-1"}


def test_guard_restores_a_captured_fake():
    structure.fetch_one = _fake_fetch_one
    repaired = restore_leaked_db_fakes()

    assert "routes.tool_api.structure.fetch_one" in repaired
    assert structure.fetch_one is db.fetch_one


def test_guard_leaves_production_bindings_alone():
    assert restore_leaked_db_fakes() == []
    assert structure.fetch_one is db.fetch_one


def test_guard_covers_every_db_helper_a_consumer_can_capture():
    """Derived, not enumerated: the guard's symbol set IS db's public callables.

    `get_db` is deliberately OUTSIDE it: the session fixture swaps `db.get_db` for the
    test-DB proxy, so it no longer resolves to a db-defined function and the guard
    cannot undo that swap mid-suite.
    """
    symbols = db_symbols()
    assert {"fetch_one", "fetch_many", "create_record", "soft_delete"} <= set(symbols)
    assert "get_db" not in symbols
    assert all(getattr(fn, "__module__", "").split(".")[0] == "db" for fn in symbols.values())


def test_guard_leaves_a_same_named_non_fake_alone():
    """Only a TEST-defined callable is a leak; a module's own `fetch_one` is not."""
    import sys

    module = type(structure)("scratch_leak_guard_module")
    module.fetch_one = db.fetch_one.__class__  # not test-defined, same name
    sys.modules["scratch_leak_guard_module"] = module
    try:
        assert restore_leaked_db_fakes() == []
        assert module.fetch_one is not db.fetch_one
    finally:
        del sys.modules["scratch_leak_guard_module"]
