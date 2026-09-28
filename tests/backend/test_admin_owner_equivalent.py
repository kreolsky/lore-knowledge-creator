"""Admin = owner-equivalent inside member projects.

Covers:
- key principals never inherit the instance role (no-regression on the
  existing live-membership re-check).
- admin elevation in get_project_access / get_document_access, the
  is_project_root predicate, and the key-no-inheritance guard.
- the owner surface (members/invites/shares) opens to admin-members via
  require_project_owner.
- note-chat moderation — admin-member is root.
- the is_owner_like payload flag + truthful my_access in every project payload.
- users.created_by provenance.

The `client` fixture wipes all tables between tests; fixtures clean their own rows
at setup for idempotency (mirrors project_with_doc).
"""

from __future__ import annotations

import hashlib
import secrets

import pytest
import pytest_asyncio
from helpers import make_token, wipe_project_children_sql
from password import hash_secret

pytestmark = pytest.mark.asyncio


# ─── Fixtures ───────────────────────────────────────────────────────────────


async def _mk_user(test_db, uid: str, name: str, email: str, role: str = "user") -> tuple[str, str]:
    from db import create_record

    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": name, "email": email,
        "password_hash": hash_secret("pw"),
        "role": role, "user_facts": "",
    })
    return uid, make_token(uid, name, role, email)


async def _grant(pid: str, uid: str, level: str) -> str:
    from db import create_record

    pm_id = f"test-ao-pm-{uid}"
    await create_record("project_members", pm_id, {
        "project_id": pid, "user_id": uid, "access_level": level,
    })
    return pm_id


async def _revoke(pm_id: str) -> None:
    from db import get_db

    db = await get_db()
    await db.query("DELETE type::record('project_members', $id)", {"id": pm_id})


async def _agent_key(test_db, user_id: str, project_id: str, scope_root: str = "") -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"test-ao-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": scope_root,
        "token_hash": token_hash, "label": "ao-agent", "capabilities": ["agent"],
        "internal": False, "auto_apply": True,
    })
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


async def _make_note(client, pid: str, doc_id: str, token: str) -> str:
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "is_note": True},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201
    return resp.json()["session_id"]


@pytest_asyncio.fixture
async def ao_world(client, test_db):
    """Owner (non-admin) + non-owner admin + stranger admin + full member, one
    private project with one doc. Returns a flat dict of ids/tokens."""
    from db import create_record

    owner_uid, owner_tok = await _mk_user(
        test_db, "test-ao-owner-001", "owner", "ao-owner@test.com", "user")
    admin_uid, admin_tok = await _mk_user(
        test_db, "test-ao-admin-001", "admin", "ao-admin@test.com", "admin")
    sadmin_uid, sadmin_tok = await _mk_user(
        test_db, "test-ao-admin-002", "sadmin", "ao-sadmin@test.com", "admin")
    member_uid, member_tok = await _mk_user(
        test_db, "test-ao-member-001", "member", "ao-member@test.com", "user")

    pid = "test-ao-proj-001"
    did = "test-ao-doc-001"
    await test_db.query(
        wipe_project_children_sql() + "; DELETE type::record('projects', $pid)",
        {"pid": pid},
    )
    await test_db.query("DELETE type::record('documents', $id)", {"id": did})
    await create_record("projects", pid, {
        "name": "AO Project", "status": "active", "project_context": "",
        "index_doc_id": did, "owner_id": owner_uid,
    })
    await create_record("documents", did, {
        "project_id": pid, "parent_id": None, "title": "doc.md",
        "content": "", "path": "doc.md",
    })
    return {
        "pid": pid, "did": did,
        "owner_uid": owner_uid, "owner_tok": owner_tok,
        "admin_uid": admin_uid, "admin_tok": admin_tok,
        "sadmin_uid": sadmin_uid, "sadmin_tok": sadmin_tok,
        "member_uid": member_uid, "member_tok": member_tok,
    }


# ─── Step 1: key principals never inherit the instance role ─────────────────
# No-regression on the EXISTING live-membership re-check (these pass today and
# must keep passing). The admin-role-inheritance guard lives in step 2.

async def test_agent_key_of_demoted_member_forbidden_on_write(client, test_db, ao_world):
    w = ao_world
    pm = await _grant(w["pid"], w["member_uid"], "full")
    try:
        key = await _agent_key(test_db, w["member_uid"], w["pid"])
        hdr = _hdr(key)
        # apply=auto: a key outside a chat session has no verdict flow to hold on,
        # so the default confirm mode 409s by design. RBAC is what this test asserts.
        body = {"title": "k", "content": "", "parent_id": w["did"],
                "node_type": "document", "apply": "auto"}
        # Still full → write allowed.
        r = await client.post("/api/tool/create_document", json=body, headers=hdr)
        assert r.status_code == 200
        # Demoted to readonly → write forbidden.
        from db import get_db
        db = await get_db()
        await db.query(
            "UPDATE type::record('project_members', $id) SET access_level = 'readonly'",
            {"id": pm},
        )
        r2 = await client.post("/api/tool/create_document", json=body, headers=hdr)
        assert r2.status_code == 403
    finally:
        await _revoke(pm)


async def test_agent_key_of_removed_member_forbidden_on_write_read_stays(client, test_db, ao_world):
    w = ao_world
    pm = await _grant(w["pid"], w["member_uid"], "full")
    try:
        from db import get_db
        db = await get_db()
        await db.query(
            "UPDATE type::record('projects', $id) SET is_public = true", {"id": w["pid"]},
        )
        await db.query("DELETE type::record('project_members', $id)", {"id": pm})
        key = await _agent_key(test_db, w["member_uid"], w["pid"])
        hdr = _hdr(key)
        # Read stays via the public branch.
        r = await client.post("/api/tool/get_project_structure", json={}, headers=hdr)
        assert r.status_code == 200
        # Write forbidden.
        r2 = await client.post(
            "/api/tool/create_document",
            json={"title": "k", "content": "", "parent_id": w["did"],
                  "node_type": "document", "apply": "auto"},
            headers=hdr,
        )
        assert r2.status_code == 403
    finally:
        await _revoke(pm)


async def test_agent_key_of_full_member_allowed_on_write(client, test_db, ao_world):
    w = ao_world
    pm = await _grant(w["pid"], w["member_uid"], "full")
    try:
        key = await _agent_key(test_db, w["member_uid"], w["pid"])
        r = await client.post(
            "/api/tool/create_document",
            json={"title": "k", "content": "", "parent_id": w["did"],
                  "node_type": "document", "apply": "auto"},
            headers=_hdr(key),
        )
        assert r.status_code == 200
    finally:
        await _revoke(pm)


# ─── Step 2: access.py elevation semantics ──────────────────────────────────

async def test_admin_member_get_project_access_elevates_full(client, test_db, ao_world):
    w = ao_world
    pm = await _grant(w["pid"], w["admin_uid"], "readonly")
    try:
        from access import get_project_access
        assert await get_project_access(
            w["pid"], {"user_id": w["admin_uid"], "role": "admin"}) == "full"
    finally:
        await _revoke(pm)


async def test_admin_public_nonmember_get_project_access_full(client, test_db, ao_world):
    w = ao_world
    from db import get_db
    db = await get_db()
    await db.query(
        "UPDATE type::record('projects', $id) SET is_public = true", {"id": w["pid"]},
    )
    from access import get_project_access
    assert await get_project_access(
        w["pid"], {"user_id": w["sadmin_uid"], "role": "admin"}) == "full"


async def test_admin_private_nonmember_get_project_access_none(client, test_db, ao_world):
    w = ao_world
    from access import get_project_access
    assert await get_project_access(
        w["pid"], {"user_id": w["sadmin_uid"], "role": "admin"}) is None


async def test_full_nonadmin_member_not_elevated(client, test_db, ao_world):
    w = ao_world
    pm = await _grant(w["pid"], w["member_uid"], "readonly")
    try:
        from access import get_project_access
        assert await get_project_access(
            w["pid"], {"user_id": w["member_uid"], "role": "user"}) == "readonly"
    finally:
        await _revoke(pm)


async def test_is_project_root_semantics(client, test_db, ao_world):
    w = ao_world
    from access import is_project_root
    from db import fetch_one
    proj = await fetch_one("projects", w["pid"])
    # Owner is root.
    assert await is_project_root(proj, {"user_id": w["owner_uid"], "role": "user"}) is True
    # Admin without a member row is NOT root on a private project.
    assert await is_project_root(proj, {"user_id": w["admin_uid"], "role": "admin"}) is False
    # Admin with a member row is root.
    pm = await _grant(w["pid"], w["admin_uid"], "readonly")
    try:
        assert await is_project_root(proj, {"user_id": w["admin_uid"], "role": "admin"}) is True
    finally:
        await _revoke(pm)
    # Full non-owner member is NOT root.
    pm2 = await _grant(w["pid"], w["member_uid"], "full")
    try:
        assert await is_project_root(proj, {"user_id": w["member_uid"], "role": "user"}) is False
    finally:
        await _revoke(pm2)


async def test_admin_agent_key_on_public_without_membership_resolves_readonly_not_full(
    client, test_db, ao_world,
):
    """The step-1 change made observable: an admin's agent key NEVER inherits the
    admin role, so the step-2 elevation never reaches it — the public branch
    resolves readonly, not full."""
    w = ao_world
    from db import get_db
    db = await get_db()
    await db.query(
        "UPDATE type::record('projects', $id) SET is_public = true", {"id": w["pid"]},
    )
    key = await _agent_key(test_db, w["admin_uid"], w["pid"])
    from api_key_auth import resolve_api_key
    ctx = await resolve_api_key(key, require="agent")
    assert ctx["user"]["role"] == "user"
    from access import get_project_access
    assert await get_project_access(ctx["project_id"], ctx["user"]) == "readonly"


# ─── Step 4: owner surface opens to admin-members ───────────────────────────

async def test_admin_member_lists_members_200(client, test_db, ao_world):
    w = ao_world
    pm = await _grant(w["pid"], w["admin_uid"], "readonly")
    try:
        r = await client.get(
            f"/api/projects/{w['pid']}/members", cookies={"lore_session": w["admin_tok"]})
        assert r.status_code == 200
    finally:
        await _revoke(pm)


async def test_admin_nonmember_lists_members_403(client, test_db, ao_world):
    w = ao_world
    r = await client.get(
        f"/api/projects/{w['pid']}/members", cookies={"lore_session": w["sadmin_tok"]})
    assert r.status_code == 403


async def test_admin_member_invites_200(client, test_db, ao_world):
    w = ao_world
    pm = await _grant(w["pid"], w["admin_uid"], "readonly")
    try:
        r = await client.post(
            f"/api/projects/{w['pid']}/members",
            json={"email": "ao-invitee@test.com", "access_level": "readonly"},
            cookies={"lore_session": w["admin_tok"]},
        )
        assert r.status_code == 200
    finally:
        await _revoke(pm)


async def test_admin_member_mints_share_link_200(client, test_db, ao_world):
    w = ao_world
    pm = await _grant(w["pid"], w["admin_uid"], "readonly")
    try:
        r = await client.post(
            f"/api/projects/{w['pid']}/documents/{w['did']}/shares",
            json={"scope": "doc"},
            cookies={"lore_session": w["admin_tok"]},
        )
        assert r.status_code == 200
        assert r.json()["share_id"]
    finally:
        await _revoke(pm)


async def test_full_nonowner_forbidden_on_every_owner_route(client, test_db, ao_world):
    """Every owner-guarded route stays 403 for a full non-owner. Pins the async
    require_project_owner rewrite — a missed `await` would silently skip a guard."""
    w = ao_world
    pm = await _grant(w["pid"], w["member_uid"], "full")
    try:
        c = {"lore_session": w["member_tok"]}
        # members list / invite / patch / remove / cancel-invite
        r = await client.get(f"/api/projects/{w['pid']}/members", cookies=c)
        assert r.status_code == 403
        r = await client.post(
            f"/api/projects/{w['pid']}/members",
            json={"email": "x@y.com", "access_level": "readonly"}, cookies=c)
        assert r.status_code == 403
        r = await client.patch(
            f"/api/projects/{w['pid']}/members/{w['admin_uid']}",
            json={"access_level": "full"}, cookies=c)
        assert r.status_code == 403
        r = await client.delete(
            f"/api/projects/{w['pid']}/members/{w['admin_uid']}", cookies=c)
        assert r.status_code == 403
        r = await client.delete(
            f"/api/projects/{w['pid']}/invites?email=x@y.com", cookies=c)
        assert r.status_code == 403
        # shares mint / scope-update / revoke (owner mints the row first)
        r = await client.post(
            f"/api/projects/{w['pid']}/documents/{w['did']}/shares",
            json={"scope": "doc"},
            cookies={"lore_session": w["owner_tok"]},
        )
        share_id = r.json()["share_id"]
        r = await client.post(
            f"/api/projects/{w['pid']}/documents/{w['did']}/shares",
            json={"scope": "doc"}, cookies=c)
        assert r.status_code == 403
        r = await client.patch(f"/api/shares/{share_id}", json={"scope": "subtree"}, cookies=c)
        assert r.status_code == 403
        r = await client.delete(f"/api/shares/{share_id}", cookies=c)
        assert r.status_code == 403
    finally:
        await _revoke(pm)


# ─── Step 5: note-chat moderation (admin-member is root) ────────────────────

async def test_admin_member_edits_others_note_200(client, test_db, ao_world):
    w = ao_world
    pm = await _grant(w["pid"], w["admin_uid"], "readonly")
    try:
        note_id = await _make_note(client, w["pid"], w["did"], w["owner_tok"])
        msg = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "owner note"}, cookies={"lore_session": w["owner_tok"]},
        )).json()
        r = await client.patch(
            f"/api/chat/messages/{msg['message_id']}",
            json={"content": "admin edited"}, cookies={"lore_session": w["admin_tok"]},
        )
        assert r.status_code == 200
        assert r.json()["content"] == "admin edited"
    finally:
        await _revoke(pm)


async def test_admin_member_deletes_others_note_cascades_200(client, test_db, ao_world):
    w = ao_world
    pm = await _grant(w["pid"], w["admin_uid"], "readonly")
    try:
        note_id = await _make_note(client, w["pid"], w["did"], w["owner_tok"])
        m1 = (await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "root"}, cookies={"lore_session": w["owner_tok"]},
        )).json()
        await client.post(
            f"/api/chat/sessions/{note_id}/messages",
            json={"content": "child", "parent_id": m1["message_id"]},
            cookies={"lore_session": w["owner_tok"]},
        )
        r = await client.delete(
            f"/api/chat/messages/{m1['message_id']}",
            cookies={"lore_session": w["admin_tok"]},
        )
        assert r.status_code == 200
        assert r.json()["deleted_count"] == 2
    finally:
        await _revoke(pm)


# ─── Step 6: is_owner_like payload flag + truthful my_access ────────────────

async def test_project_list_admin_member_owner_like_true(client, test_db, ao_world):
    w = ao_world
    pm = await _grant(w["pid"], w["admin_uid"], "readonly")
    try:
        r = await client.get("/api/projects", cookies={"lore_session": w["admin_tok"]})
        assert r.status_code == 200
        p = next(p for p in r.json() if p["project_id"] == w["pid"])
        assert p["is_owner_like"] is True
        assert p["my_access"] == "full"
    finally:
        await _revoke(pm)


async def test_project_list_admin_public_nonmember_owner_like_false(client, test_db, ao_world):
    w = ao_world
    from db import get_db
    db = await get_db()
    await db.query(
        "UPDATE type::record('projects', $id) SET is_public = true", {"id": w["pid"]},
    )
    r = await client.get("/api/projects", cookies={"lore_session": w["sadmin_tok"]})
    assert r.status_code == 200
    p = next(p for p in r.json() if p["project_id"] == w["pid"])
    assert p["is_owner_like"] is False
    assert p["my_access"] == "full"


async def test_admin_list_all_projects_private_nonmember_my_access_none(client, test_db, ao_world):
    w = ao_world
    r = await client.get("/api/admin/projects", cookies={"lore_session": w["sadmin_tok"]})
    assert r.status_code == 200
    p = next(p for p in r.json() if p["project_id"] == w["pid"])
    assert p["my_access"] is None
    assert p["is_owner_like"] is False


# ─── /api/admin/projects?q= — server-side name filter (section-shell step 2) ─


async def test_admin_projects_q_case_insensitive_substring(client, test_db, ao_world):
    """q matches by lowercase substring in either case."""
    w = ao_world
    for q in ("o pro", "AO PROJ"):
        r = await client.get(f"/api/admin/projects?q={q}", cookies={"lore_session": w["sadmin_tok"]})
        assert r.status_code == 200
        assert w["pid"] in [p["project_id"] for p in r.json()], q
    r_miss = await client.get("/api/admin/projects?q=zzz-no-such", cookies={"lore_session": w["sadmin_tok"]})
    assert r_miss.status_code == 200
    assert r_miss.json() == []


async def test_admin_projects_q_matches_owner_name(client, test_db, ao_world):
    """q also matches the owner's user name ("whose projects"), case-insensitive;
    the owner name is not the project name, so a name-only filter would miss it."""
    w = ao_world
    assert "owner" not in "AO Project".lower()
    for q in ("owner", "OWN"):
        r = await client.get(f"/api/admin/projects?q={q}", cookies={"lore_session": w["sadmin_tok"]})
        assert r.status_code == 200
        assert w["pid"] in [p["project_id"] for p in r.json()], q
    # A member's name is not an owner: no match by membership.
    await _grant(w["pid"], w["member_uid"], "readonly")
    r = await client.get("/api/admin/projects?q=member", cookies={"lore_session": w["sadmin_tok"]})
    assert r.status_code == 200
    assert w["pid"] not in [p["project_id"] for p in r.json()]


async def test_admin_projects_empty_q_returns_all(client, test_db, ao_world):
    """An explicitly empty q is the unfiltered list, not a 422."""
    w = ao_world
    r = await client.get("/api/admin/projects?q=", cookies={"lore_session": w["sadmin_tok"]})
    assert r.status_code == 200
    assert w["pid"] in [p["project_id"] for p in r.json()]


async def test_admin_projects_none_name_does_not_raise(client, test_db, ao_world):
    """A project row whose name reads as NONE survives every q mode.

    `projects.name` is TYPE string and enforced on every write form (CONTENT,
    SET, INSERT — probed), so a nameless row can only exist via schema
    evolution: a row created while the field definition is absent keeps
    reading NONE after the field is (re)defined — the ref_image_preview
    backfill trap shape. string::lowercase(NONE) then raises InternalError,
    and without the `?? ''` guard one such row would 500 the whole admin
    list. The REMOVE/DEFINE round-trip here manufactures exactly that row.
    """
    w = ao_world
    noname_id = "test-ao-proj-noname-001"
    await test_db.query("DELETE type::record('projects', $id)", {"id": noname_id})
    try:
        await test_db.query("REMOVE FIELD IF EXISTS name ON projects")
        await test_db.query(
            "CREATE type::record('projects', $id) CONTENT {"
            " status: 'active', owner_id: $uid"
            "}",
            {"id": noname_id, "uid": w["owner_uid"]},
        )
    finally:
        # Same definition as schema.surql — DEFINE does not retro-validate the
        # nameless row, which is the point.
        await test_db.query(
            "DEFINE FIELD IF NOT EXISTS name ON projects TYPE string"
        )
    try:
        r_all = await client.get("/api/admin/projects", cookies={"lore_session": w["sadmin_tok"]})
        assert r_all.status_code == 200
        ids_all = [p["project_id"] for p in r_all.json()]
        assert noname_id in ids_all
        assert w["pid"] in ids_all

        r_q = await client.get("/api/admin/projects?q=zzz", cookies={"lore_session": w["sadmin_tok"]})
        assert r_q.status_code == 200
        assert noname_id not in [p["project_id"] for p in r_q.json()]
    finally:
        await test_db.query("DELETE type::record('projects', $id)", {"id": noname_id})


async def test_admin_projects_q_non_admin_still_403(client, test_db, ao_world):
    """The q parameter does not soften require_admin."""
    w = ao_world
    r = await client.get("/api/admin/projects?q=xx", cookies={"lore_session": w["member_tok"]})
    assert r.status_code == 403


async def test_single_project_read_owner_like(client, test_db, ao_world):
    w = ao_world
    pm = await _grant(w["pid"], w["admin_uid"], "readonly")
    try:
        r = await client.get(
            f"/api/projects/{w['pid']}", cookies={"lore_session": w["admin_tok"]})
        assert r.status_code == 200
        proj = r.json()["project"]
        assert proj["is_owner_like"] is True
        assert proj["my_access"] == "full"
    finally:
        await _revoke(pm)


async def test_patch_project_response_owner_like(client, test_db, ao_world):
    w = ao_world
    r = await client.patch(
        f"/api/projects/{w['pid']}", json={"name": "Renamed"},
        cookies={"lore_session": w["owner_tok"]},
    )
    assert r.status_code == 200
    assert r.json()["is_owner_like"] is True


async def test_create_project_is_owner_like_true(client, test_db, ao_world):
    w = ao_world
    r = await client.post(
        "/api/projects", json={"name": "New AO"},
        cookies={"lore_session": w["owner_tok"]},
    )
    assert r.status_code == 200
    assert r.json()["is_owner_like"] is True


async def test_open_document_bundle_project_owner_like(client, test_db, ao_world):
    w = ao_world
    pm = await _grant(w["pid"], w["admin_uid"], "readonly")
    try:
        r = await client.get(
            f"/api/documents/open/{w['did']}", cookies={"lore_session": w["admin_tok"]})
        assert r.status_code == 200
        proj = r.json()["project"]
        assert proj["is_owner_like"] is True
    finally:
        await _revoke(pm)


# ─── Step 8: users.created_by provenance ────────────────────────────────────

async def test_create_user_admin_stamps_created_by(client, admin_user, test_db, ao_world):
    _, admin_tok = admin_user
    r = await client.post(
        "/api/admin/users",
        json={"name": "created", "email": "ao-created@test.com", "password": "password1", "role": "user"},
        cookies={"lore_session": admin_tok},
    )
    assert r.status_code == 200
    from db import fetch_one
    row = await fetch_one("users", r.json()["user_id"])
    assert row.get("created_by") == admin_user[0]


async def test_user_created_outside_admin_route_carries_none(client, test_db, ao_world):
    """A user whose row was not written by create_user_admin (seed/migrations)
    carries NONE — the moderator's group is only the accounts an admin created."""
    from db import create_record, fetch_one

    uid = "test-ao-noprovenance-001"
    await create_record("users", uid, {
        "name": "legacy", "email": "ao-legacy@test.com",
        "password_hash": hash_secret("pw"), "role": "user", "user_facts": "",
    })
    row = await fetch_one("users", uid)
    assert row.get("created_by") in (None, "NONE")
