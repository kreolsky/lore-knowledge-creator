"""Migration: messages_schemafull_converge — dev's `messages` takes prod's mode.

`messages` was `TYPE ANY SCHEMALESS` on dev while prod and gray were SCHEMAFULL, and
that divergence is what hid the gen_steps chip loss: the identical write stored on dev
and was refused on prod, so the defect could only be found in production.

The migration cannot just flip the mode. `ALTER TABLE … SCHEMAFULL` succeeds over rows
carrying an undeclared key WITHOUT re-coercing them, and the row only becomes unwritable
later — the next per-row UPDATE fails with `Found field 'entry_id', …`. Dev carried
exactly that: 1497 rows still holding the retired `entry_id`. So the strip runs first and
the flip second, and the binding assertion below is not the table's mode but that a row
which used to carry the retired key can be written again.
"""

import pytest

from db import get_db
from migrations.migrate_messages_schemafull_converge import (
    _migrate_messages_schemafull_converge,
)

_POISONED = "converge-poisoned-row"
_CLEAN = "converge-clean-row"


@pytest.fixture
async def schemaless_messages_with_a_retired_key(test_db):
    """Rebuild the pre-migration reality: a SCHEMALESS `messages` holding a row with
    `entry_id` — the state no fresh test DB has, and the only state this migration
    has anything to do."""
    db = await get_db()
    await db.query("ALTER TABLE messages SCHEMALESS")
    for rid in (_POISONED, _CLEAN):
        await db.query("DELETE type::record('messages', $id)", {"id": rid})
    await db.query(
        'CREATE type::record("messages", $id) SET chat_id = "c", role = "assistant", '
        'content = "x", entry_id = "legacy-1";',
        {"id": _POISONED},
    )
    await db.query(
        'CREATE type::record("messages", $id) SET chat_id = "c", role = "assistant", '
        'content = "y";',
        {"id": _CLEAN},
    )
    yield db
    for rid in (_POISONED, _CLEAN):
        await db.query("DELETE type::record('messages', $id)", {"id": rid})
    await db.query("ALTER TABLE messages SCHEMAFULL")


async def test_converge_flips_the_mode_and_leaves_every_row_writable(
    schemaless_messages_with_a_retired_key,
):
    db = schemaless_messages_with_a_retired_key

    before = await db.query("SELECT entry_id FROM type::record('messages', $id)", {"id": _POISONED})
    assert before[0].get("entry_id") == "legacy-1", "the precondition did not build"

    await _migrate_messages_schemafull_converge(db)

    definition = (await db.query("INFO FOR DB"))["tables"]["messages"]
    assert "SCHEMAFULL" in definition, f"messages did not converge ({definition!r})"

    rows = await db.query("SELECT entry_id FROM type::record('messages', $id)", {"id": _POISONED})
    assert not rows[0].get("entry_id"), "the retired key survived the sweep"

    # The point of the whole migration: this UPDATE is what fails on a converged table
    # whose rows were never swept, and it is what a user hits editing an old message.
    await db.query(
        "UPDATE type::record('messages', $id) SET content = 'touched'", {"id": _POISONED}
    )
    after = await db.query("SELECT content FROM type::record('messages', $id)", {"id": _POISONED})
    assert after[0]["content"] == "touched"


async def test_converge_is_idempotent_on_an_already_converged_table(
    schemaless_messages_with_a_retired_key,
):
    """The second run is the one every already-SCHEMAFULL database (prod, gray, a fresh
    test DB) actually gets — it must be a no-op, not an error."""
    db = schemaless_messages_with_a_retired_key

    await _migrate_messages_schemafull_converge(db)
    await _migrate_messages_schemafull_converge(db)

    definition = (await db.query("INFO FOR DB"))["tables"]["messages"]
    assert "SCHEMAFULL" in definition
    rows = await db.query("SELECT content FROM type::record('messages', $id)", {"id": _CLEAN})
    assert rows[0]["content"] == "y", "an untouched row was disturbed"
