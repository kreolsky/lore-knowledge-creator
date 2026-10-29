"""Group audience sharing — access cascade, dashboard predicate, PATCH guards.

A project opens beyond its member rows via `audiences`: a list of
{subject, access_level} rows ('public', 'mod:<uid>', 'group:<id>'); effective
access is the MAX over (member row, every matching audience row). Wire and
admin-group rows live in test_unified_audiences.py; live revocation in
test_group_audience_live.py; the frontend is NOT this file.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from helpers import make_token
from password import hash_secret

_MOD = "ga-mod-1"
_MOD2 = "ga-mod-2"


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
        "password_hash": hash_secret("grouppass"),
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


async def _mk_project(
    test_db, pid: str, owner_uid: str, *, audiences: list[dict] | None = None,
) -> None:
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


async def _mk_member(test_db, pid: str, uid: str, level: str) -> None:
    from db import create_record
    await create_record("project_members", str(uuid4()), {
        "project_id": pid,
        "user_id": uid,
        "access_level": level,
    })


async def _project_row(test_db, pid: str) -> dict:
    rows = await test_db.query(
        "SELECT audiences FROM projects WHERE id = type::record('projects', $pid)",
        {"pid": pid},
    )
    assert rows, f"project {pid} missing"
    return rows[0]


def _row(subject: str, level: str) -> dict:
    return {"subject": subject, "access_level": level}


@pytest_asyncio.fixture
async def group_cast(test_db):
    """The cast: moderator M, group members U1/U2, no-group user X, group owner O."""
    await _mk_user(test_db, _MOD, role="moderator")
    await _mk_user(test_db, "ga-u1", moderator_id=_MOD)
    await _mk_user(test_db, "ga-u2", moderator_id=_MOD)
    await _mk_user(test_db, "ga-x")
    await _mk_user(test_db, "ga-owner", moderator_id=_MOD)
    yield


# ─── Dashboard list: conditional audience predicate (the NONE trap) ──────────


@pytest.mark.asyncio
async def test_group_member_sees_audience_project_with_my_access(
    client, test_db, group_cast,
):
    pid = "ga-proj-list2"
    await _mk_project(test_db, pid, "ga-owner",
                      audiences=[_row(f"mod:{_MOD}", "commentator")])
    resp = await client.get(
        "/api/projects", cookies={"lore_session": make_token("ga-u1", "u1", "user")},
    )
    assert resp.status_code == 200
    target = next(p for p in resp.json() if p["project_id"] == pid)
    assert target["my_access"] == "commentator"
    # max over a member row too: u2 holds a readonly row + commentator audience
    await _mk_member(test_db, pid, "ga-u2", "readonly")
    resp = await client.get(
        "/api/projects", cookies={"lore_session": make_token("ga-u2", "u2", "user")},
    )
    target = next(p for p in resp.json() if p["project_id"] == pid)
    assert target["my_access"] == "commentator"


# ─── PATCH guards: public row admin-only, own-subject rule ────────────────────

@pytest.mark.asyncio
async def test_non_admin_owner_cannot_add_public_row(client, test_db, group_cast):
    """The 'public' subject is an instance-wide broadcast — an operator
    decision; a non-admin owner's add is refused and the project stays private."""
    pid = "ga-proj-guard1"
    await _mk_project(test_db, pid, "ga-owner")
    resp = await client.patch(
        f"/api/projects/{pid}", json={"audiences": [_row("public", "readonly")]},
        cookies={"lore_session": make_token("ga-owner", "owner", "user")},
    )
    assert resp.status_code == 403
    assert (await _project_row(test_db, pid))["audiences"] == []


@pytest.mark.asyncio
async def test_owner_sets_own_group_subject_and_foreign_is_refused(
    client, test_db, group_cast,
):
    pid = "ga-proj-guard3"
    await _mk_project(test_db, pid, "ga-owner")
    owner = {"lore_session": make_token("ga-owner", "owner", "user")}
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"audiences": [_row(f"mod:{_MOD}", "commentator")]},
        cookies=owner,
    )
    assert resp.status_code == 200
    assert (await _project_row(test_db, pid))["audiences"] == [_row(f"mod:{_MOD}", "commentator")]
    # a foreign subject is refused
    await _mk_user(test_db, _MOD2, role="moderator")
    resp = await client.patch(
        f"/api/projects/{pid}", json={"audiences": [_row(f"mod:{_MOD2}", "readonly")]},
        cookies=owner,
    )
    assert resp.status_code == 403
    # clearing is allowed
    resp = await client.patch(
        f"/api/projects/{pid}", json={"audiences": []}, cookies=owner,
    )
    assert resp.status_code == 200
    assert (await _project_row(test_db, pid))["audiences"] == []


# The old pair-semantics tests (audience-without-level → readonly default;
# lone group_access → 422) are gone with the pair: every audiences row carries
# its level — a level-less row 422s (test_unified_audiences.py
# test_row_missing_access_level_is_422) and the readonly default lives in the
# migration (test_migrate_audiences_to_list.py test_audience_only_defaults_readonly_level).


@pytest.mark.asyncio
async def test_owner_cannot_set_an_admin_group_subject(client, test_db, group_cast):
    """Admin groups (`group:<id>`) are an admin write — a non-admin owner who
    belongs to one still may set only their own MODERATOR group's subject."""
    from db import create_record
    gid = "ga-admin-group"
    await test_db.query("DELETE group_members WHERE group_id = $g", {"g": gid})
    await create_record("group_members", str(uuid4()), {"group_id": gid, "user_id": "ga-owner"})
    pid = "ga-proj-guard9"
    await _mk_project(test_db, pid, "ga-owner")
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"audiences": [_row(f"group:{gid}", "readonly")]},
        cookies={"lore_session": make_token("ga-owner", "owner", "user")},
    )
    assert resp.status_code == 403
    assert (await _project_row(test_db, pid))["audiences"] == []
