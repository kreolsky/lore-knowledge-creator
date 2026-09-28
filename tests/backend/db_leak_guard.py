"""Unbind test fakes captured by a production module's `from db import X`.

The class: `monkeypatch.setattr("db.fetch_one", fake)` rebinds the `db` module only.
A consumer that did `from db import fetch_one` at module scope resolved the name ONCE
at import — so a consumer FIRST imported INSIDE the patched window binds the fake
permanently, and monkeypatch's undo cannot see it. The fake then answers every later
test in the process.

Measured: `test_apply_create_table_*` (agent table writes import `routes.tool_api.*`
lazily, inside the call) leaked a `{"project_id": "p-1"}` fetch_one into
`routes.tool_api.structure`; every `move_document` test sharing that process then
404'd as cross-project. Same class as the `get_db` sweep in conftest's session
fixture — this one runs per test because the capture happens at first import,
whenever that falls.

Lives OUTSIDE conftest deliberately: a test importing `conftest` re-executes its
module body, which re-opens the suite flock and aborts the run.
"""

from __future__ import annotations

import sys


def _is_test_module(name: str) -> bool:
    return name.split(".")[-1].startswith(("test_", "conftest"))


def _is_test_defined(obj) -> bool:
    """True when `obj` was defined in a test module — its fakes are what leak."""
    return _is_test_module(getattr(obj, "__module__", "") or "")


def db_symbols() -> dict:
    """The `db` public callables a consumer can capture via `from db import X`.

    Derived from the module, not enumerated: a new db helper joins the guard the
    moment it exists, and a renamed one cannot leave a stale literal behind.
    """
    import db as db_module
    return {
        name: obj for name in dir(db_module) if not name.startswith("_")
        if callable(obj := getattr(db_module, name, None))
        and getattr(obj, "__module__", "").split(".")[0] == "db"
    }


def restore_leaked_db_fakes() -> list[str]:
    """Rebind every captured test fake back to the live db symbol.

    Returns what it repaired (`module.name`), so the guard itself is testable.
    """
    repaired: list[str] = []
    live_symbols = db_symbols()
    for mod in list(sys.modules.values()):
        if mod is None or _is_test_module(getattr(mod, "__name__", "")):
            continue
        for name, live in live_symbols.items():
            held = getattr(mod, name, None)
            if held is not None and held is not live and _is_test_defined(held):
                setattr(mod, name, live)
                repaired.append(f"{mod.__name__}.{name}")
    return repaired
