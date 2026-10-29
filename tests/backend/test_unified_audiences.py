"""Unified audiences — the multi-row project audience list (plan unified-project-audience, step 1).

A project carries `audiences`: [{subject, access_level}], where subject is
'public' (every logged-in user; readonly|commentator only, admin-only write),
'mod:<uid>' (a moderator group) or 'group:<id>' (an admin group). Effective
access = MAX over member row and every audience row the user's subjects match.
Refused principals first (the guard), then the cascade, the list predicate and
the live group-membership revocations. The migration has its own file
(test_migrate_audiences_to_list.py).
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
import pytest_asyncio
from helpers import join_collab_ws, make_token, project_collab_url
from password import hash_secret

_MOD = "ua-mod"
_MOD2 = "ua-mod2"
_X = "ua-x"
_OWNER = "ua-owner"
_G1 = "ua-g1"
_G2 = "ua-g2"
_G_MISSING = "ua-g-missing"


async def _mk_user(test_db, uid: str, *, role: str = "user", moderator_id: str | None = None) -> None:
    """Seed a user (delete-first so tests are self-cleaning)."""
    from db import create_record
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    content = {
        "name": uid,
        "email": f"{uid}@ua-test.com",
        "password_hash": hash_secret("uapass"),
        "role": role,
        "user_facts": "",
    }
    if moderator_id:
        content["moderator_id"] = moderator_id
    await create_record("users", uid, content)


async def _mk_group(test_db, gid: str, member_uids: list[str]) -> None:
    """Seed an admin group with members (delete-first)."""
    from db import create_record
    await test_db.query("DELETE group_members WHERE group_id = $g", {"g": gid})
    await test_db.query("DELETE type::record('groups', $id)", {"id": gid})
    await create_record("groups", gid, {"name": gid})
    for uid in member_uids:
        await create_record("group_members", str(uuid4()), {"group_id": gid, "user_id": uid})


async def _mk_project(test_db, pid: str, owner_uid: str, audiences: list[dict] | None = None) -> None:
    """Seed a project (delete-first, members wiped with it)."""
    from db import create_record
    await test_db.query("DELETE project_members WHERE project_id = $p", {"p": pid})
    await test_db.query("DELETE type::record('projects', $id)", {"id": pid})
    await create_record("projects", pid, {
        "name": pid,
        "status": "active",
        "project_context": "",
        "owner_id": owner_uid,
        "audiences": audiences or [],
    })


async def _audiences(test_db, pid: str) -> list[dict]:
    rows = await test_db.query(
        "SELECT VALUE audiences FROM type::record('projects', $pid)",
        {"pid": pid},
    )
    assert rows, f"project {pid} missing"
    return rows[0] or []


@pytest_asyncio.fixture
async def ua_cast(test_db):
    """Moderator M (+ a second moderator M2); group members U1/U2; two admin
    groups G1 (U3) / G2 (U3); no-group X; group owner O (in M's group)."""
    await _mk_user(test_db, _MOD, role="moderator")
    await _mk_user(test_db, _MOD2, role="moderator")
    await _mk_user(test_db, "ua-u1", moderator_id=_MOD)
    await _mk_user(test_db, "ua-u2", moderator_id=_MOD)
    await _mk_user(test_db, "ua-u3")
    await _mk_user(test_db, _X)
    await _mk_user(test_db, _OWNER, moderator_id=_MOD)
    await _mk_group(test_db, _G1, ["ua-u3"])
    await _mk_group(test_db, _G2, ["ua-u3"])
    yield


def _row(subject: str, level: str) -> dict:
    return {"subject": subject, "access_level": level}


# ─── Guard: refused principals ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_non_admin_adding_public_row_is_403(client, test_db, ua_cast):
    pid = "ua-proj-guard1"
    await _mk_project(test_db, pid, _OWNER)
    resp = await client.patch(
        f"/api/projects/{pid}", json={"audiences": [_row("public", "readonly")]},
        cookies={"lore_session": make_token(_OWNER, "owner", "user")},
    )
    assert resp.status_code == 403
    assert await _audiences(test_db, pid) == []


@pytest.mark.asyncio
async def test_full_non_owner_member_cannot_remove_public_row(client, test_db, ua_cast):
    """The audience write path is owner/admin only — a full non-owner member
    cannot strip or add rows (mirrors _audience_writer)."""
    from db import create_record
    pid = "ua-proj-guard2"
    await _mk_project(test_db, pid, _OWNER, [_row("public", "readonly")])
    await create_record("project_members", str(uuid4()), {
        "project_id": pid, "user_id": "ua-u1", "access_level": "full",
    })
    resp = await client.patch(
        f"/api/projects/{pid}", json={"audiences": []},
        cookies={"lore_session": make_token("ua-u1", "u1", "user")},
    )
    assert resp.status_code == 403
    assert await _audiences(test_db, pid) == [_row("public", "readonly")]


@pytest.mark.asyncio
async def test_admin_dead_moderator_or_missing_group_is_422(client, test_db, ua_cast, admin_user):
    _, admin_token = admin_user
    pid = "ua-proj-guard4"
    await _mk_project(test_db, pid, _OWNER)
    # mod:<uid of a row that is NOT role=moderator> → 422
    resp = await client.patch(
        f"/api/projects/{pid}", json={"audiences": [_row("mod:ua-u1", "readonly")]},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 422
    # group:<no such groups row> → 422
    resp = await client.patch(
        f"/api/projects/{pid}", json={"audiences": [_row(f"group:{_G_MISSING}", "readonly")]},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_non_admin_owner_foreign_add_403_remove_200(client, test_db, ua_cast, admin_user):
    """A non-admin owner may not ADD/CHANGE a foreign row (any subject but
    their own group), but may REMOVE any row — including admin-set ones."""
    _, admin_token = admin_user
    pid = "ua-proj-guard5"
    owner = {"lore_session": make_token(_OWNER, "owner", "user")}
    await _mk_project(test_db, pid, _OWNER, [
        _row("public", "readonly"), _row(f"group:{_G1}", "full"),
    ])
    # adding a foreign mod: row (another moderator's group) → 403
    resp = await client.patch(
        f"/api/projects/{pid}", json={"audiences": [_row(f"mod:{_MOD2}", "readonly")]},
        cookies=owner,
    )
    assert resp.status_code == 403
    # adding a foreign group: row → 403
    resp = await client.patch(
        f"/api/projects/{pid}", json={"audiences": [_row(f"group:{_G2}", "readonly")]},
        cookies=owner,
    )
    assert resp.status_code == 403
    # removing a foreign group: row (keeping the others) → 200
    resp = await client.patch(
        f"/api/projects/{pid}", json={"audiences": [_row("public", "readonly")]},
        cookies=owner,
    )
    assert resp.status_code == 200
    assert await _audiences(test_db, pid) == [_row("public", "readonly")]
    # removing the admin-set public row → 200 (the old is_public 403 is dropped
    # deliberately: narrowing one's own project never weakens security)
    resp = await client.patch(f"/api/projects/{pid}", json={"audiences": []}, cookies=owner)
    assert resp.status_code == 200
    assert await _audiences(test_db, pid) == []


# ─── Cascade: member row + audience rows → MAX ────────────────────────────────

@pytest.mark.asyncio
async def test_admin_group_row_member_gets_level_non_member_none(client, test_db, ua_cast):
    from access import get_project_access
    pid = "ua-proj-casc1"
    await _mk_project(test_db, pid, _OWNER, [_row(f"group:{_G1}", "commentator")])
    assert await get_project_access(pid, {"user_id": "ua-u3", "role": "user"}) == "commentator"
    assert await get_project_access(pid, {"user_id": _X, "role": "user"}) is None


# ─── Dashboard list: the conditional audience predicate ──────────────────────


@pytest.mark.asyncio
async def test_deleted_user_gets_no_public_access(client, test_db, ua_cast):
    """The empty-list trap: user_subjects of a deleted user is [], so no
    audience row matches — 'public' included (the HTTP list never gets that
    far: auth 401s a deleted user first; the live consumer is the collab
    recompute, which reads live rows)."""
    from access import get_project_access
    pid = "ua-proj-list-del"
    await _mk_project(test_db, pid, _OWNER, [_row("public", "readonly")])
    await _mk_user(test_db, "ua-dead")
    await test_db.query(
        "UPDATE type::record('users', $id) SET deleted_at = time::now()",
        {"id": "ua-dead"},
    )
    assert await get_project_access(pid, {"user_id": "ua-dead", "role": "user"}) is None


# ─── Live revocation: admin group membership changes ─────────────────────────

async def _mk_doc(client, token: str, pid: str) -> str:
    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": "Doc", "content": "hi"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


def _recv_until(ws, msg_type: str, *, limit: int = 20) -> dict:
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        if msg.get("type") == msg_type:
            return msg
    raise AssertionError(f"no {msg_type!r} frame within {limit} frames")


async def _settle() -> None:
    await asyncio.sleep(0.5)


@pytest.mark.asyncio
async def test_put_group_members_removal_revokes_collab_client(
    sync_app, client, test_db, ua_cast, admin_user,
):
    """Removing a user from an admin group that carries an audience row must
    reach their OPEN collab client at once (a project-wide access_changed)."""
    _, admin_token = admin_user
    pid = "ua-proj-live1"
    await _mk_project(test_db, pid, _OWNER, [_row(f"group:{_G1}", "full")])
    doc_id = await _mk_doc(client, make_token(_OWNER, "owner", "user"), pid)
    with sync_app.websocket_connect(
        project_collab_url(pid), cookies={"lore_session": make_token("ua-u3", "u3", "user")},
    ) as ws:
        join_collab_ws(ws, doc_id)
        json.loads(ws.receive_text())  # init
        resp = await client.put(
            f"/api/admin/groups/{_G1}/members", json={"user_ids": []},
            cookies={"lore_session": admin_token},
        )
        assert resp.status_code == 200, resp.text
        await _settle()
        assert _recv_until(ws, "access_revoked")


@pytest.mark.asyncio
async def test_delete_group_strips_row_and_revokes(
    sync_app, client, test_db, ua_cast, admin_user,
):
    _, admin_token = admin_user
    pid = "ua-proj-live3"
    await _mk_project(test_db, pid, _OWNER, [
        _row("public", "readonly"), _row(f"group:{_G1}", "full"),
    ])
    doc_id = await _mk_doc(client, make_token(_OWNER, "owner", "user"), pid)
    with sync_app.websocket_connect(
        project_collab_url(pid), cookies={"lore_session": make_token("ua-u3", "u3", "user")},
    ) as ws:
        join_collab_ws(ws, doc_id)
        json.loads(ws.receive_text())  # init
        resp = await client.delete(
            f"/api/admin/groups/{_G1}", cookies={"lore_session": admin_token},
        )
        assert resp.status_code == 200, resp.text
        await _settle()
        # the group row is gone; the public row remains → downgrade, not revoke
        msg = _recv_until(ws, "access_changed")
        assert msg["level"] == "readonly"
    assert await _audiences(test_db, pid) == [_row("public", "readonly")]
