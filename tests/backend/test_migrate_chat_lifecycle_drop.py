"""The lifecycle drop migration: chat_sessions.lifecycle is REMOVEd
idempotently (plan agent-line-harness-lifecycle step 9 — the dual-run flag
retired with the SSE arm; the driver owns every turn).
"""
from unittest.mock import AsyncMock

from migrations.migrate_chat_lifecycle_drop import _migrate_chat_lifecycle_drop


async def test_removes_exactly_the_lifecycle_field():
    db = AsyncMock()
    await _migrate_chat_lifecycle_drop(db)
    sqls = [c.args[0] for c in db.query.call_args_list]
    assert sqls == [
        "REMOVE FIELD IF EXISTS lifecycle ON chat_sessions",
    ]


def test_registered_in_registry():
    from migrations.runner import _MIGRATIONS

    names = [name for name, _ in _MIGRATIONS]
    assert "chat_lifecycle_drop" in names
