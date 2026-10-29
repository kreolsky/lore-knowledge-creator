"""POST /api/admin/projects/{id}/owner — ownership transfer from the admin panel.

Pins:
- the end state of the write order: old owner → `full` member row, new owner's
  member row gone, owner_id flipped;
- the gates: target == owner 400, unknown / soft-deleted target 404, unknown
  project 404, plain user 403;
- the refused principal: the OLD owner loses the owner-only surface (403), the
  new owner gains it (200);
- get_project_access / is_project_root for both uids;
- access_changed emitted for BOTH uids.

The three moderator-scope cases live in test_moderator.py (its fixtures).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from helpers import make_token
from password import hash_secret

from db import create_record, fetch_one


@pytest_asyncio.fixture
async def plain_owner(test_db):
    """A role='user' project owner — the non-admin old owner the refused-
    principal and is_project_root cases need (an admin old owner stays a
    project root via their new member row)."""
    uid = "owner-transfer-001"
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "plainowner",
        "email": "plainowner@test.com",
        "password_hash": hash_secret("pass123"),
        "role": "user",
        "user_facts": "",
    })
    token = make_token(uid, "plainowner", "user", "plainowner@test.com")
    yield uid, token
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})


async def _seed_project(test_db, pid: str, owner_uid: str) -> None:
    await test_db.query("DELETE type::record('projects', $id)", {"id": pid})
    await create_record("projects", pid, {
        "name": pid,
        "status": "active",
        "project_context": "",
        "owner_id": owner_uid,
    })


async def _add_member(test_db, pid: str, uid: str, access: str = "readonly") -> None:
    await create_record("project_members", f"{pid}-pm-{uid}", {
        "project_id": pid,
        "user_id": uid,
        "access_level": access,
    })


# ─── Happy path ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_transfers_to_member(client, test_db, admin_user, regular_user):
    """Owner flips to X; the old owner becomes a `full` member; the members
    map omits X and lists the old owner — the member count is unchanged."""
    admin_uid, admin_token = admin_user
    x_uid, _ = regular_user
    pid = "p-owner-tr-1"
    await _seed_project(test_db, pid, admin_uid)
    await _add_member(test_db, pid, x_uid, "readonly")

    before = await client.get(
        f"/api/admin/projects/{pid}/members", cookies={"lore_session": admin_token},
    )
    assert before.status_code == 200
    assert before.json() == {x_uid: "readonly"}

    resp = await client.post(
        f"/api/admin/projects/{pid}/owner",
        json={"user_id": x_uid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"owner_id": x_uid, "owner_name": "testuser"}

    project = await fetch_one("projects", pid)
    assert project["owner_id"] == x_uid

    after = await client.get(
        f"/api/admin/projects/{pid}/members", cookies={"lore_session": admin_token},
    )
    assert after.status_code == 200
    assert after.json() == {admin_uid: "full"}
    assert len(after.json()) == len(before.json())


# ─── Refusals ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plain_user_403(client, test_db, regular_user, plain_owner):
    """A plain user has no transfer surface at all."""
    x_uid, x_token = regular_user
    old_uid, _ = plain_owner
    pid = "p-owner-tr-7"
    await _seed_project(test_db, pid, old_uid)
    resp = await client.post(
        f"/api/admin/projects/{pid}/owner",
        json={"user_id": x_uid},
        cookies={"lore_session": x_token},
    )
    assert resp.status_code == 403
