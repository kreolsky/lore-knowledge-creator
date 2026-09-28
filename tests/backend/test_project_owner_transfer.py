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

import asyncio

import pytest
import pytest_asyncio
from helpers import make_token
from password import hash_secret

from access import get_project_access, is_project_root
from db import create_record, fetch_one
from event_bus import off, on


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


@pytest.mark.asyncio
async def test_old_owner_refused_new_owner_allowed(
    client, test_db, admin_user, regular_user, plain_owner,
):
    """After the flip the OLD owner (a plain user) loses the owner-only member
    list (403) and X gains it (200)."""
    _, admin_token = admin_user
    x_uid, x_token = regular_user
    old_uid, old_token = plain_owner
    pid = "p-owner-tr-3"
    await _seed_project(test_db, pid, old_uid)
    await _add_member(test_db, pid, x_uid, "commentator")

    resp = await client.post(
        f"/api/admin/projects/{pid}/owner",
        json={"user_id": x_uid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/api/projects/{pid}/members", cookies={"lore_session": old_token},
    )
    assert resp.status_code == 403
    resp = await client.get(
        f"/api/projects/{pid}/members", cookies={"lore_session": x_token},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_access_and_root_resolution(
    client, test_db, admin_user, regular_user, plain_owner,
):
    """Both resolve to `full`; is_project_root is True for X, False for the
    old plain owner."""
    _, admin_token = admin_user
    x_uid, _ = regular_user
    old_uid, _ = plain_owner
    pid = "p-owner-tr-4"
    await _seed_project(test_db, pid, old_uid)
    await _add_member(test_db, pid, x_uid, "readonly")

    resp = await client.post(
        f"/api/admin/projects/{pid}/owner",
        json={"user_id": x_uid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text

    project = await fetch_one("projects", pid)
    for uid in (x_uid, old_uid):
        user = {"user_id": uid, "role": "user"}
        assert await get_project_access(pid, user) == "full"
    assert await is_project_root(project, {"user_id": x_uid, "role": "user"}) is True
    assert await is_project_root(project, {"user_id": old_uid, "role": "user"}) is False


@pytest.mark.asyncio
async def test_transfer_emits_access_changed_for_both(
    client, test_db, admin_user, regular_user,
):
    """The event every member mutation emits, for BOTH uids at `full`."""
    admin_uid, admin_token = admin_user
    x_uid, _ = regular_user
    pid = "p-owner-tr-5"
    await _seed_project(test_db, pid, admin_uid)
    await _add_member(test_db, pid, x_uid, "readonly")

    captured: list[tuple[str, str]] = []

    def handler(**kw):
        captured.append((kw.get("user_id"), kw.get("access")))

    on("access_changed", handler)
    try:
        resp = await client.post(
            f"/api/admin/projects/{pid}/owner",
            json={"user_id": x_uid},
            cookies={"lore_session": admin_token},
        )
        assert resp.status_code == 200, resp.text
        await asyncio.sleep(0.2)  # the bus spawns subscribers as tasks
    finally:
        off("access_changed", handler)

    assert sorted(captured) == sorted([(x_uid, "full"), (admin_uid, "full")])


# ─── Refusals ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refusals(client, test_db, admin_user, regular_user):
    """target == owner → 400; unknown user → 404; soft-deleted user → 404;
    unknown project → 404."""
    admin_uid, admin_token = admin_user
    x_uid, _ = regular_user
    pid = "p-owner-tr-6"
    await _seed_project(test_db, pid, admin_uid)

    resp = await client.post(
        f"/api/admin/projects/{pid}/owner",
        json={"user_id": admin_uid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Already the owner"

    resp = await client.post(
        f"/api/admin/projects/{pid}/owner",
        json={"user_id": "no-such-user"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 404

    gone_uid = "owner-transfer-gone"
    await create_record("users", gone_uid, {
        "name": "gone", "email": "gone@test.com",
        "password_hash": "x", "role": "user", "user_facts": "",
    })
    await test_db.query(
        "UPDATE type::record('users', $id) SET deleted_at = time::now()",
        {"id": gone_uid},
    )
    resp = await client.post(
        f"/api/admin/projects/{pid}/owner",
        json={"user_id": gone_uid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 404

    resp = await client.post(
        "/api/admin/projects/p-owner-tr-nope/owner",
        json={"user_id": x_uid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 404


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


@pytest.mark.asyncio
async def test_admin_transfer_repairs_ownerless_project(client, test_db, admin_user, regular_user):
    """An ownerless project (owner_id is option<string>) gets X as owner; no
    member row is written for the missing previous owner."""
    _, admin_token = admin_user
    x_uid, _ = regular_user
    pid = "p-owner-tr-8"
    await test_db.query("DELETE type::record('projects', $id)", {"id": pid})
    await create_record("projects", pid, {
        "name": pid, "status": "active", "project_context": "",
    })
    await _add_member(test_db, pid, x_uid, "readonly")

    resp = await client.post(
        f"/api/admin/projects/{pid}/owner",
        json={"user_id": x_uid},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    assert (await fetch_one("projects", pid))["owner_id"] == x_uid
    rows = await test_db.query(
        "SELECT user_id FROM project_members WHERE project_id = $pid", {"pid": pid},
    )
    assert rows == []
