"""Nested-object round-trips over the two fields whose DDL drifted (2026-07-28).

`documents` and `user_preferences` were declared SCHEMAFULL in surreal/schema.surql while
both tables have always been SCHEMALESS in every environment — `DEFINE TABLE IF NOT EXISTS`
is a no-op on an existing table, so the declaration never applied. The consequence was
silent: `FLEXIBLE` is legal only on a SCHEMAFULL table, so the `file_meta` and
`preferences` definitions failed on EVERY boot (swallowed by apply_schema as a warning).

The fix declares the real mode and drops the illegal keyword. That is only correct if a
plain `TYPE option<object>` still preserves arbitrary children on a SCHEMALESS table —
this file binds that. The `preferences` half lives in test_preferences.py, over the HTTP
endpoint; this is the `documents.file_meta` half, against the live DB.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _reset_db_connection():
    """Each test runs on its own event loop, but the SurrealDB client singleton binds its
    connection to the first loop it sees — reset so this test reconnects on its own."""
    from db import reset_db

    await reset_db()
    yield
    await reset_db()


async def test_file_meta_preserves_arbitrary_nested_object():
    """An arbitrarily deep, heterogeneous `file_meta` survives a DB round-trip.

    Deliberately mixes the shapes the real media types produce (image width/height, audio
    duration, a nested dict and a list) plus a level of nesting no code writes today — the
    point is that the field accepts children the schema never enumerates, which is what the
    now-removed FLEXIBLE keyword was there to guarantee.
    """
    from db import get_db

    db = await get_db()
    doc_id = "schema-drift-file-meta-smoke"
    # No None values anywhere: SurrealDB stores NONE as an ABSENT key, so a null would
    # come back missing and say nothing about schema strictness — the thing under test.
    file_meta = {
        "mime_type": "image/jpeg",
        "original_name": "IMG_20220719_100304.jpg",
        "file_size": 1233426,
        "width": 4000,
        "height": 1936,
        "duration": 12.5,
        "nested": {"exif": {"lens": "50mm", "tags": ["a", "b"]}, "pages": [1, 2, 3]},
    }
    try:
        await db.query(
            'CREATE type::record("documents", $id) SET project_id = "p", path = "/x", '
            "title = \"x\", file_meta = $fm;",
            {"id": doc_id, "fm": file_meta},
        )

        rows = await db.query(
            "SELECT file_meta FROM type::record('documents', $id)", {"id": doc_id}
        )
        assert rows, "document row was not created"
        assert rows[0]["file_meta"] == file_meta, (
            "file_meta was flattened, coerced or partially dropped — a plain "
            "TYPE option<object> must keep arbitrary children on a SCHEMALESS table"
        )
    finally:
        await db.query("DELETE type::record('documents', $id)", {"id": doc_id})


async def test_drifted_tables_converge_to_the_declared_mode():
    """The live tables must actually BE what schema.surql declares.

    Asserts over the LIVE definition rather than a literal in the DDL file — the failure
    this catches is precisely a file that disagrees with the database, which is how the
    drift went unnoticed: `DEFINE TABLE IF NOT EXISTS` froze each environment's mode at
    database-creation time, so prod sat SCHEMALESS while every test DB was SCHEMAFULL and
    no test could tell. The `ALTER TABLE` lines in schema.surql are what converge them.
    """
    from db import get_db

    db = await get_db()
    info = await db.query("INFO FOR DB")
    for table in ("documents", "user_preferences"):
        definition = info["tables"][table]
        assert "SCHEMAFULL" in definition, (
            f"{table} is not SCHEMAFULL ({definition!r}) — the ALTER TABLE line for it in "
            "surreal/schema.surql did not apply"
        )


async def test_gen_steps_keeps_the_detached_runs_child_keys():
    """A `messages.gen_steps` array survives a round-trip with its children intact.

    The detached generate_image run's two chips live only in this column — the driver's
    log never sees the background task, and the refined SD prompt exists nowhere else — so
    a rejected child key is not a degraded write, it is the chips missing from every
    reload. `messages` is SCHEMAFULL here and on prod, and on a SCHEMAFULL table the
    parent's FLEXIBLE does not reach the array's ELEMENTS: without an explicit
    `gen_steps.*` FLEXIBLE definition SurrealDB materializes them as a plain `TYPE object`
    and refuses every child, which is what silently dropped both chips on prod.

    The step dicts below carry the real keys image_gen/persist.py writes plus one no code
    writes today — the point is that the column accepts children the schema never
    enumerates.
    """
    from db import get_db

    db = await get_db()
    msg_id = "schema-drift-gen-steps-smoke"
    gen_steps = [
        # The two chips exactly as image_gen/persist.py::_persist_and_announce builds them.
        {
            "tool_call_id": "gen:run1:refine",
            "tool": "refine_prompt",
            "summary": "refine prompt",
            "detail": "a lone tower at dusk, volumetric light",
        },
        {
            "tool_call_id": "gen:run1",
            "tool": "generate_image",
            "summary": "generate image",
            "image_ref_ids": ["ref-a", "ref-b"],
            "run_id": "run1",
            "title": "Tower",
            # Not written by any code today: the column must take children the schema
            # never enumerates, which is the whole point of FLEXIBLE on the elements.
            "unenumerated": {"deep": ["x", 1]},
        },
    ]
    try:
        await db.query(
            'CREATE type::record("messages", $id) SET chat_id = "c", role = "assistant", '
            "content = \"x\", gen_steps = $gs;",
            {"id": msg_id, "gs": gen_steps},
        )

        rows = await db.query(
            "SELECT gen_steps FROM type::record('messages', $id)", {"id": msg_id}
        )
        assert rows, "message row was not created"
        assert rows[0]["gen_steps"] == gen_steps, (
            "gen_steps lost or coerced a child key — the refiner chip and the image chip "
            "are unrenderable after a reload when this does not hold"
        )
    finally:
        await db.query("DELETE type::record('messages', $id)", {"id": msg_id})
