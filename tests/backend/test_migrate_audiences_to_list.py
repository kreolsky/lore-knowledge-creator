"""audiences_to_list migration — the trio folds into the multi-row list.

Stages the LEGACY schema itself (per-test fixture) (is_public / group_audience / group_access +
idx_projects_group_audience): the live test DB is built from the current
schema.surql, which no longer defines them — exactly the fresh-DB boot this
migration's early-return path covers.

Also pins the NONE-backfill mechanic (INVARIANT(corruption) at the audiences
DEFINE in schema.surql): a row predating the field holds NONE and fails
coercion on its next UPDATE until the backfill heals it.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from migrations.migrate_audiences_to_list import _migrate_audiences_to_list

_LEGACY_DEFINE = [
    "DEFINE FIELD IF NOT EXISTS is_public ON projects TYPE bool DEFAULT false",
    "DEFINE FIELD IF NOT EXISTS group_audience ON projects TYPE option<string>",
    "DEFINE FIELD IF NOT EXISTS group_access ON projects TYPE option<string>",
    "DEFINE INDEX IF NOT EXISTS idx_projects_group_audience ON projects FIELDS group_audience",
]
_AUDIENCES_DEFINE = [
    "DEFINE FIELD audiences ON projects TYPE array<object> DEFAULT []",
    "DEFINE FIELD OVERWRITE audiences.* ON projects TYPE object",
    "DEFINE FIELD OVERWRITE audiences.*.subject ON projects TYPE string",
    "DEFINE FIELD OVERWRITE audiences.*.access_level ON projects TYPE string "
    "ASSERT $value IN ['readonly','commentator','full']",
]


async def _define_legacy(test_db) -> None:
    """Stage the legacy trio AND backfill it: on prod every row predating the
    trio is gone — the backfill mirrors that invariant, and without it a row
    left key-less by an earlier REMOVE fails the non-option bool coercion on
    the migration's fold UPDATE (the INVARIANT(corruption) trap itself)."""
    for stmt in _LEGACY_DEFINE:
        await test_db.query(stmt)
    await test_db.query("UPDATE projects SET is_public = false WHERE is_public IS NONE")


async def _drop_legacy(test_db) -> None:
    await test_db.query("REMOVE INDEX IF EXISTS idx_projects_group_audience ON projects")
    for f in ("is_public", "group_audience", "group_access"):
        await test_db.query(f"REMOVE FIELD IF EXISTS {f} ON projects")
    await test_db.query("UPDATE projects UNSET is_public, group_audience, group_access")


@pytest_asyncio.fixture
async def legacy_trio(test_db):
    """Legacy trio staged for the test, dropped after — even on failure."""
    await _define_legacy(test_db)
    yield test_db
    await _drop_legacy(test_db)


async def _row(test_db, pid: str) -> dict:
    rows = await test_db.query(
        "SELECT * FROM type::record('projects', $pid)", {"pid": pid},
    )
    assert rows, f"project {pid} missing"
    return rows[0]


async def _mk_legacy_project(test_db, pid: str, *, is_public=False, group_audience=None, group_access=None) -> None:
    from db import create_record
    await test_db.query("DELETE type::record('projects', $id)", {"id": pid})
    content = {
        "name": pid, "status": "active", "project_context": "",
        "owner_id": "ua-mig-owner", "is_public": is_public,
    }
    if group_audience is not None:
        content["group_audience"] = group_audience
        content["group_access"] = group_access
    await create_record("projects", pid, content)


async def _fields(test_db) -> dict:
    info = await test_db.query("INFO FOR TABLE projects")
    blob = info[0] if isinstance(info, list) else info
    return (blob or {}).get("fields") or {}


# ─── Data mapping ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_public_only_becomes_public_row(test_db, legacy_trio):
    await _mk_legacy_project(test_db, "ua-mig-pub", is_public=True)
    await _migrate_audiences_to_list(test_db)
    row = await _row(test_db, "ua-mig-pub")
    assert row["audiences"] == [{"subject": "public", "access_level": "readonly"}]


@pytest.mark.asyncio
async def test_audience_only_defaults_readonly_level(test_db, legacy_trio):
    await _mk_legacy_project(test_db, "ua-mig-aud", group_audience="mod:m1")
    await _mk_legacy_project(test_db, "ua-mig-aud-lvl", group_audience="mod:m2", group_access="full")
    await _migrate_audiences_to_list(test_db)
    assert (await _row(test_db, "ua-mig-aud"))["audiences"] == [
        {"subject": "mod:m1", "access_level": "readonly"},
    ]
    assert (await _row(test_db, "ua-mig-aud-lvl"))["audiences"] == [
        {"subject": "mod:m2", "access_level": "full"},
    ]


@pytest.mark.asyncio
async def test_both_become_two_rows_in_order(test_db, legacy_trio):
    await _mk_legacy_project(test_db, "ua-mig-both", is_public=True,
                             group_audience="mod:m1", group_access="commentator")
    await _migrate_audiences_to_list(test_db)
    assert (await _row(test_db, "ua-mig-both"))["audiences"] == [
        {"subject": "public", "access_level": "readonly"},
        {"subject": "mod:m1", "access_level": "commentator"},
    ]


@pytest.mark.asyncio
async def test_neither_becomes_empty_and_select_star_carries_no_legacy_keys(test_db, legacy_trio):
    await _mk_legacy_project(test_db, "ua-mig-none")
    await _mk_legacy_project(test_db, "ua-mig-priv")
    await _migrate_audiences_to_list(test_db)
    for pid in ("ua-mig-none", "ua-mig-priv"):
        row = await _row(test_db, pid)
        assert row["audiences"] == []
        # UNSET removed the stored keys: nothing stale reaches the client via SELECT *
        for key in ("is_public", "group_audience", "group_access"):
            assert key not in row, f"{pid} still carries {key}"


@pytest.mark.asyncio
async def test_already_migrated_row_untouched(test_db, legacy_trio):
    await _mk_legacy_project(test_db, "ua-mig-done")
    await test_db.query(
        "UPDATE type::record('projects', $p) SET audiences = "
        "[{subject: 'public', access_level: 'commentator'}]",
        {"p": "ua-mig-done"},
    )
    await _migrate_audiences_to_list(test_db)
    assert (await _row(test_db, "ua-mig-done"))["audiences"] == [
        {"subject": "public", "access_level": "commentator"},
    ]


@pytest.mark.asyncio
async def test_idempotent_second_run(test_db, legacy_trio):
    await _mk_legacy_project(test_db, "ua-mig-again", is_public=True)
    await _migrate_audiences_to_list(test_db)
    await _migrate_audiences_to_list(test_db)
    row = await _row(test_db, "ua-mig-again")
    assert row["audiences"] == [{"subject": "public", "access_level": "readonly"}]


# ─── Schema effect: definitions and index gone ───────────────────────────────

@pytest.mark.asyncio
async def test_definitions_and_index_removed_from_info(test_db, legacy_trio):
    await _mk_legacy_project(test_db, "ua-mig-schema")
    await _migrate_audiences_to_list(test_db)
    fields = await _fields(test_db)
    for f in ("is_public", "group_audience", "group_access"):
        assert f not in fields, f
    assert "audiences" in fields and "audiences.*.subject" in fields
    info = await test_db.query("INFO FOR TABLE projects")
    blob = info[0] if isinstance(info, list) else info
    assert "idx_projects_group_audience" not in ((blob or {}).get("indexes") or {})


@pytest.mark.asyncio
async def test_fresh_db_boot_is_a_noop(test_db):
    """No legacy fields defined (the normal test-DB state): the data pass is
    skipped and the REMOVE IF EXISTS statements are no-ops."""
    from db import create_record
    await test_db.query("DELETE type::record('projects', $id)", {"id": "ua-mig-fresh"})
    await create_record("projects", "ua-mig-fresh", {
        "name": "fresh", "status": "active", "project_context": "",
        "owner_id": "ua-mig-owner",
    })
    await _migrate_audiences_to_list(test_db)
    row = await _row(test_db, "ua-mig-fresh")
    assert row["audiences"] == []


# ─── The NONE backfill mechanic (INVARIANT(corruption)) ──────────────────────

@pytest.mark.asyncio
async def test_row_predating_field_reads_empty_after_backfill(test_db):
    """Stage the prod sequence ON projects: a row created while `audiences` is
    undefined holds NONE after the DEFINE; the schema's own backfill heals it
    to []; until then any UPDATE on the row fails coercion."""
    await test_db.query("DELETE type::record('projects', $id)", {"id": "ua-none-row"})
    for stmt in (
        "REMOVE FIELD IF EXISTS audiences.*.access_level ON projects",
        "REMOVE FIELD IF EXISTS audiences.*.subject ON projects",
        "REMOVE FIELD IF EXISTS audiences.* ON projects",
        "REMOVE FIELD IF EXISTS audiences ON projects",
    ):
        await test_db.query(stmt)
    await test_db.query(
        "CREATE type::record('projects', 'ua-none-row') SET name = 'none', "
        "status = 'active', project_context = '', owner_id = 'ua-mig-owner'",
    )
    for stmt in _AUDIENCES_DEFINE:
        await test_db.query(stmt)
    row = await _row(test_db, "ua-none-row")
    assert row.get("audiences") is None, "row predating the field must read NONE"
    with pytest.raises(Exception):
        await test_db.query(
            "UPDATE type::record('projects', $p) SET name = 'boom'", {"p": "ua-none-row"},
        )
    # apply_schema's pass (the same statements schema.surql carries) heals it
    import pathlib

    from db import split_schema_statements
    for stmt in split_schema_statements(pathlib.Path("/surreal/schema.surql").read_text()):
        await test_db.query(stmt)
    assert (await _row(test_db, "ua-none-row"))["audiences"] == []
    await test_db.query(
        "UPDATE type::record('projects', $p) SET name = 'healed'", {"p": "ua-none-row"},
    )
    await test_db.query("DELETE type::record('projects', $id)", {"id": "ua-none-row"})


# ─── public_to_group_audience early return on a field-less DB ────────────────

@pytest.mark.asyncio
async def test_public_to_group_audience_early_returns_without_the_field(test_db):
    from migrations.migrate_public_to_group_audience import (
        _migrate_public_to_group_audience,
    )
    # the test DB carries no is_public field definition (current schema) — the
    # legacy SELECT would filter on an undefined field; the early return skips
    assert "is_public" not in await _fields(test_db)
    await _migrate_public_to_group_audience(test_db)  # must not raise
