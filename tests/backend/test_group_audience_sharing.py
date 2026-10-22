"""Group audience sharing — roster, client flags, group-restricted invites.

GET /api/me/group-roster,
`group_subject` + `can_publish_project` in the login / /me payloads, and
invite_member restricted to the roster for a group-bound non-admin owner
(admin and no-group owners keep the silent free-text invite). The cascade,
PATCH guards and live revocation live in test_group_audience*.py.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from helpers import make_token
from password import hash_secret

_MOD = "gas-mod-1"
_MOD2 = "gas-mod-2"
_PW = "grouppass"


async def _mk_user(
    test_db, uid: str, *, role: str = "user", moderator_id: str | None = None,
    deleted: bool = False,
) -> None:
    """Seed a user (delete-first so tests are self-cleaning)."""
    from db import create_record
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    content = {
        "name": uid,
        "email": f"{uid}@test.com",
        "password_hash": hash_secret(_PW),
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


async def _mk_project(test_db, pid: str, owner_uid: str) -> None:
    """Seed a private project (delete-first, members/invites wiped with it)."""
    from db import create_record
    await test_db.query("DELETE project_members WHERE project_id = $p", {"p": pid})
    await test_db.query("DELETE pending_invites WHERE project_id = $p", {"p": pid})
    await test_db.query("DELETE type::record('projects', $id)", {"id": pid})
    await create_record("projects", pid, {
        "name": pid,
        "status": "active",
        "project_context": "",
        "owner_id": owner_uid,
    })


async def _member_rows(test_db, pid: str) -> dict[str, str]:
    rows = await test_db.query(
        "SELECT user_id, access_level FROM project_members WHERE project_id = $p",
        {"p": pid},
    )
    return {r["user_id"]: r["access_level"] for r in (rows or [])}


@pytest_asyncio.fixture
async def group_cast(test_db):
    """M (moderator) with U1/U2 (+soft-deleted U3); a foreign group MOD2 with
    Y; X without a group; O in M's group (the project owner)."""
    await _mk_user(test_db, _MOD, role="moderator")
    await _mk_user(test_db, _MOD2, role="moderator")
    await _mk_user(test_db, "gas-u1", moderator_id=_MOD)
    await _mk_user(test_db, "gas-u2", moderator_id=_MOD)
    await _mk_user(test_db, "gas-dead", moderator_id=_MOD, deleted=True)
    await _mk_user(test_db, "gas-y", moderator_id=_MOD2)
    await _mk_user(test_db, "gas-x")
    await _mk_user(test_db, "gas-owner", moderator_id=_MOD)
    yield


# ─── Roster: GET /api/me/group-roster ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_roster_lists_moderator_and_peers_not_self(client, test_db, group_cast):
    """U1's roster = {moderator, every peer incl. the owner} — never self,
    never a deleted member, never another group's member."""
    resp = await client.get(
        "/api/me/group-roster",
        cookies={"lore_session": make_token("gas-u1", "u1", "user")},
    )
    assert resp.status_code == 200
    rows = resp.json()
    uids = {r["user_id"] for r in rows}
    assert uids == {_MOD, "gas-u2", "gas-owner"}
    for r in rows:
        assert set(r.keys()) == {"user_id", "name", "email"}


@pytest.mark.asyncio
async def test_moderator_roster_is_own_members(client, test_db, group_cast):
    """A moderator's roster = their group's members (moderator themself excluded)."""
    resp = await client.get(
        "/api/me/group-roster",
        cookies={"lore_session": make_token(_MOD, "m", "moderator")},
    )
    assert resp.status_code == 200
    uids = {r["user_id"] for r in resp.json()}
    assert uids == {"gas-u1", "gas-u2", "gas-owner"}


@pytest.mark.asyncio
async def test_roster_empty_for_no_group_user_and_admin(
    client, test_db, group_cast, admin_user,
):
    """No-group user and admin both get [] — no enumeration oracle."""
    resp = await client.get(
        "/api/me/group-roster",
        cookies={"lore_session": make_token("gas-x", "x", "user")},
    )
    assert resp.status_code == 200
    assert resp.json() == []
    _, admin_token = admin_user
    resp = await client.get(
        "/api/me/group-roster", cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ─── Client flags: group_subject + can_publish_project ────────────────────────

@pytest.mark.asyncio
async def test_login_and_me_carry_group_subject_and_flags(
    client, test_db, group_cast, admin_user,
):
    """group_subject rides login and /me (member → mod:<M>, moderator →
    mod:<own uid>, no-group → null); can_publish_project is admin-only."""
    resp = await client.post(
        "/api/auth/login", json={"email": "gas-u1@test.com", "password": _PW},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["group_subject"] == f"mod:{_MOD}"
    assert data["can_publish_project"] is False

    resp = await client.get(
        "/api/auth/me", cookies={"lore_session": make_token("gas-u1", "u1", "user")},
    )
    assert resp.status_code == 200
    assert resp.json()["group_subject"] == f"mod:{_MOD}"

    resp = await client.get(
        "/api/auth/me", cookies={"lore_session": make_token(_MOD, "m", "moderator")},
    )
    assert resp.json()["group_subject"] == f"mod:{_MOD}"

    resp = await client.get(
        "/api/auth/me", cookies={"lore_session": make_token("gas-x", "x", "user")},
    )
    assert resp.json()["group_subject"] is None

    resp = await client.post(
        "/api/auth/login", json={"email": "admin@test.com", "password": "adminpass"},
    )
    data = resp.json()
    assert data["can_publish_project"] is True
    assert data["group_subject"] is None


@pytest.mark.asyncio
async def test_me_group_subject_fresh_after_group_move(
    client, test_db, group_cast, admin_user,
):
    """/me re-reads the group pointer — a moderator_id change lands on the
    next /me, not at token expiry."""
    _, admin_token = admin_user
    tok = {"lore_session": make_token("gas-u1", "u1", "user")}
    resp = await client.get("/api/auth/me", cookies=tok)
    assert resp.json()["group_subject"] == f"mod:{_MOD}"
    resp = await client.patch(
        "/api/admin/users/gas-u1", json={"moderator_id": _MOD2},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    resp = await client.get("/api/auth/me", cookies=tok)
    assert resp.json()["group_subject"] == f"mod:{_MOD2}"


# ─── Invite: roster-only for a group-bound owner ──────────────────────────────

@pytest.mark.asyncio
async def test_group_owner_invite_outsider_refused(client, test_db, group_cast):
    """A group-bound owner inviting a registered outsider → 403, no member
    row, no pending row."""
    pid = "gas-proj-inv1"
    await _mk_project(test_db, pid, "gas-owner")
    resp = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "gas-x@test.com", "access_level": "commentator"},
        cookies={"lore_session": make_token("gas-owner", "o", "user")},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only members of your group"
    assert await _member_rows(test_db, pid) == {}
    rows = await test_db.query(
        "SELECT id FROM pending_invites WHERE project_id = $p", {"p": pid},
    )
    assert not rows


@pytest.mark.asyncio
async def test_group_owner_invite_unknown_email_refused_no_pending(
    client, test_db, group_cast,
):
    """An UNREGISTERED email is 403 too — the group boundary outranks the
    silent-invite queue (nothing is queued)."""
    pid = "gas-proj-inv2"
    await _mk_project(test_db, pid, "gas-owner")
    resp = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "nobody@test.com", "access_level": "readonly"},
        cookies={"lore_session": make_token("gas-owner", "o", "user")},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only members of your group"
    rows = await test_db.query(
        "SELECT id FROM pending_invites WHERE project_id = $p", {"p": pid},
    )
    assert not rows


@pytest.mark.asyncio
async def test_group_owner_invites_peer_and_moderator(client, test_db, group_cast):
    """Roster emails pass the gate: a peer and the moderator land member rows."""
    pid = "gas-proj-inv3"
    await _mk_project(test_db, pid, "gas-owner")
    owner = {"lore_session": make_token("gas-owner", "o", "user")}
    resp = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "gas-u2@test.com", "access_level": "commentator"},
        cookies=owner,
    )
    assert resp.status_code == 200
    assert resp.json() == {"queued": True}
    resp = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "gas-mod-1@test.com", "access_level": "readonly"},
        cookies=owner,
    )
    assert resp.status_code == 200
    assert await _member_rows(test_db, pid) == {
        "gas-u2": "commentator", _MOD: "readonly",
    }


@pytest.mark.asyncio
async def test_moderator_owner_invites_group_only(client, test_db, group_cast):
    """A moderator owner may invite their own members; a foreign group's
    member is refused even though registered."""
    pid = "gas-proj-inv4"
    await _mk_project(test_db, pid, _MOD)
    mod = {"lore_session": make_token(_MOD, "m", "moderator")}
    resp = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "gas-u1@test.com", "access_level": "full"},
        cookies=mod,
    )
    assert resp.status_code == 200
    assert await _member_rows(test_db, pid) == {"gas-u1": "full"}
    resp = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "gas-y@test.com", "access_level": "readonly"},
        cookies=mod,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only members of your group"
    assert await _member_rows(test_db, pid) == {"gas-u1": "full"}


@pytest.mark.asyncio
async def test_no_group_owner_silent_invite_queues(client, test_db, group_cast):
    """A no-group owner keeps today's silent free-text invite: an unknown
    email queues a pending row."""
    pid = "gas-proj-inv5"
    await _mk_project(test_db, pid, "gas-x")
    resp = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "stranger@test.com", "access_level": "readonly"},
        cookies={"lore_session": make_token("gas-x", "x", "user")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"queued": True}
    rows = await test_db.query(
        "SELECT email FROM pending_invites WHERE project_id = $p", {"p": pid},
    )
    assert rows and rows[0]["email"] == "stranger@test.com"


@pytest.mark.asyncio
async def test_admin_owner_free_text_invite_unchanged(
    client, test_db, group_cast, admin_user,
):
    """An admin owner is exempt from the roster gate: unknown email queues,
    any registered email lands a member row."""
    admin_uid, admin_token = admin_user
    pid = "gas-proj-inv6"
    await _mk_project(test_db, pid, admin_uid)
    admin = {"lore_session": admin_token}
    resp = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "anyone@test.com", "access_level": "readonly"},
        cookies=admin,
    )
    assert resp.status_code == 200
    assert resp.json() == {"queued": True}
    resp = await client.post(
        f"/api/projects/{pid}/members",
        json={"email": "gas-x@test.com", "access_level": "commentator"},
        cookies=admin,
    )
    assert resp.status_code == 200
    assert await _member_rows(test_db, pid) == {"gas-x": "commentator"}
