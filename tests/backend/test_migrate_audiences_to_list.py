"""audiences_to_list migration — the trio folds into the multi-row list.

Stages the LEGACY schema itself (per-test fixture) (is_public / group_audience / group_access +
idx_projects_group_audience): the live test DB is built from the current
schema.surql, which no longer defines them — exactly the fresh-DB boot this
migration's early-return path covers.

Also pins the NONE-backfill line (INVARIANT(corruption) at the audiences
DEFINE in schema.surql): a row holding NONE is healed to [] by the schema pass.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from helpers import migrate_existing_rows

from migrations.migrate_audiences_to_list import _migrate_audiences_to_list

_LEGACY_DEFINE = [
    "DEFINE FIELD IF NOT EXISTS is_public ON projects TYPE bool DEFAULT false{migrate}",
    "DEFINE FIELD IF NOT EXISTS group_audience ON projects TYPE option<string>",
    "DEFINE FIELD IF NOT EXISTS group_access ON projects TYPE option<string>",
    "DEFINE INDEX IF NOT EXISTS idx_projects_group_audience ON projects FIELDS group_audience",
]


async def _define_legacy(test_db) -> None:
    """Stage the legacy trio AND backfill it: on prod no row predates the trio.
    The MIGRATE clause fills rows left key-less by an earlier REMOVE — v4
    refuses the non-option bool DEFINE over them without it."""
    for stmt in _LEGACY_DEFINE:
        await test_db.query(stmt.replace("{migrate}", migrate_existing_rows("false")))


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


# ─── Data mapping ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_both_become_two_rows_in_order(test_db, legacy_trio):
    await _mk_legacy_project(test_db, "ua-mig-both", is_public=True,
                             group_audience="mod:m1", group_access="commentator")
    await _migrate_audiences_to_list(test_db)
    assert (await _row(test_db, "ua-mig-both"))["audiences"] == [
        {"subject": "public", "access_level": "readonly"},
        {"subject": "mod:m1", "access_level": "commentator"},
    ]

