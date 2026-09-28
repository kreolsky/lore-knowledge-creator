"""The apply-lock drop migration: the two retired chat_sessions fields are
REMOVEd idempotently (plan remove-chat-branch-lock — the chat branch lock is
deleted; edit and fork stay available for the life of a session).
"""
from unittest.mock import AsyncMock

from migrations.migrate_chat_apply_lock_fields_drop import (
    _migrate_chat_apply_lock_fields_drop,
)


async def test_removes_exactly_the_two_apply_lock_fields():
    db = AsyncMock()
    await _migrate_chat_apply_lock_fields_drop(db)
    sqls = [c.args[0] for c in db.query.call_args_list]
    assert sqls == [
        "REMOVE FIELD IF EXISTS has_applied_edit ON chat_sessions",
        "REMOVE FIELD IF EXISTS frozen_anchor_message_id ON chat_sessions",
    ]


def test_registered_in_registry():
    from migrations.runner import _MIGRATIONS

    names = [name for name, _ in _MIGRATIONS]
    assert "chat_apply_lock_fields_drop" in names
