"""public_to_group_audience: is_public=true projects migrate to a group audience.

Per owner kind:
- live non-admin owner with moderator_id → is_public=false,
  group_audience='mod:<moderator_id>', group_access='readonly'
- live moderator owner → 'mod:<own uid>', readonly
- soft-deleted owner or owner without a group → private (no audience)
- admin owner → untouched (is_public stays admin-only public)

The legacy trio (is_public/group_audience/group_access) is staged per-test:
the live schema no longer defines it (audiences_to_list removed it), and this
migration exists precisely for DBs that still carry it. On a field-less DB
(the fresh-DB boot) it early-returns without touching anything.
"""

from __future__ import annotations

import pytest
from password import hash_secret

from migrations.migrate_public_to_group_audience import (
    _migrate_public_to_group_audience,
)

_MOD = "ga-mig-mod"
_MEMBER = "ga-mig-user"
_MOD_OWNER = "ga-mig-modown"
_DEAD_OWNER = "ga-mig-dead"
_NOGROUP_OWNER = "ga-mig-nogroup"
_ADMIN_OWNER = "test-admin-001"

_LEGACY_DEFINE = [
    "DEFINE FIELD IF NOT EXISTS is_public ON projects TYPE bool DEFAULT false",
    "DEFINE FIELD IF NOT EXISTS group_audience ON projects TYPE option<string>",
    "DEFINE FIELD IF NOT EXISTS group_access ON projects TYPE option<string>",
]


async def _define_legacy(test_db) -> None:
    for stmt in _LEGACY_DEFINE:
        await test_db.query(stmt)


async def _drop_legacy(test_db) -> None:
    for f in ("is_public", "group_audience", "group_access"):
        await test_db.query(f"REMOVE FIELD IF EXISTS {f} ON projects")


async def _mk_user(test_db, uid: str, *, role: str = "user",
                   moderator_id: str | None = None, deleted: bool = False) -> None:
    from db import create_record
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    content = {
        "name": uid,
        "email": f"{uid}@test.com",
        "password_hash": hash_secret("migpass"),
        "role": role,
        "user_facts": "",
    }
    if moderator_id:
        content["moderator_id"] = moderator_id
    await create_record("users", uid, content)
    if deleted:
        await test_db.query(
            "UPDATE type::record('users', $id) SET deleted_at = time::now()",
            {"id": uid},
        )


async def _mk_public_project(test_db, pid: str, owner_uid: str) -> None:
    from db import create_record
    await test_db.query("DELETE type::record('projects', $id)", {"id": pid})
    await create_record("projects", pid, {
        "name": pid,
        "status": "active",
        "project_context": "",
        "owner_id": owner_uid,
        "is_public": True,
    })


async def _row(test_db, pid: str) -> dict:
    rows = await test_db.query(
        "SELECT is_public, group_audience, group_access FROM projects "
        "WHERE id = type::record('projects', $pid)",
        {"pid": pid},
    )
    assert rows, f"project {pid} missing"
    return rows[0]


@pytest.fixture(autouse=True)
async def _seed(test_db, admin_user):
    """The legacy trio staged, then the five owner kinds, one public project each."""
    await _define_legacy(test_db)
    await _mk_user(test_db, _MOD, role="moderator")
    await _mk_user(test_db, _MEMBER, moderator_id=_MOD)
    await _mk_user(test_db, _MOD_OWNER, role="moderator")
    await _mk_user(test_db, _DEAD_OWNER, moderator_id=_MOD, deleted=True)
    await _mk_user(test_db, _NOGROUP_OWNER)
    # admin_user fixture seeds test-admin-001 (role admin)
    for suffix, owner in (
        ("member", _MEMBER), ("modown", _MOD_OWNER), ("dead", _DEAD_OWNER),
        ("nogroup", _NOGROUP_OWNER), ("admin", _ADMIN_OWNER),
    ):
        await _mk_public_project(test_db, f"ga-mig-proj-{suffix}", owner)
    yield test_db
    for suffix in ("member", "modown", "dead", "nogroup", "admin"):
        await test_db.query(
            "DELETE type::record('projects', $id)", {"id": f"ga-mig-proj-{suffix}"},
        )
    await _drop_legacy(test_db)


@pytest.mark.asyncio
async def test_soft_deleted_owner_goes_private(test_db):
    await _migrate_public_to_group_audience(test_db)
    row = await _row(test_db, "ga-mig-proj-dead")
    assert row["is_public"] is False
    assert row["group_audience"] is None
    assert row["group_access"] is None
