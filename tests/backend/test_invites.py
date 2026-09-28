"""Registration invite links — mint binding, public register, single-use, TTL.

POST /api/invites
(admin | moderator) mints a single-use 3-day token bound to a moderator's
group; GET/POST /api/register/{token} are public, ride the login rate limiter,
and a successful POST creates a role='user' account (created_by = inviter,
moderator_id = the invite's binding), flushes pending project invites for the
email, and lands the new user logged in via the session cookie.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from helpers import make_token
from password import hash_secret

from db import create_record

INVITE_EMAIL = "invited@test.com"


@pytest_asyncio.fixture
async def moderator_user(test_db):
    """A role='moderator' user — the group head (same shape as test_moderator)."""
    uid = "test-mod-inv"
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "modinv",
        "email": "modinv@test.com",
        "password_hash": hash_secret("pass123"),
        "role": "moderator",
        "user_facts": "",
    })
    token = make_token(uid, "modinv", "moderator", "modinv@test.com")
    yield uid, token
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})


async def _fetch_invite(test_db, token: str) -> dict | None:
    rows = await test_db.query(
        "SELECT * FROM registration_invites WHERE token_hash = $h LIMIT 1",
        {"h": hashlib.sha256(token.encode()).hexdigest()},
    )
    return rows[0] if rows else None


async def _mint(client, token: str, body: dict | None = None) -> str:
    """Mint an invite with the given session token; returns the raw invite
    token (the /register URL is composed client-side from the minter's page
    origin — the mint never returns a URL)."""
    resp = await client.post("/api/invites", json=body or {}, cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# ─── Mint ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mint_requires_manager_role(client, regular_user):
    """A plain user cannot mint invites."""
    _, token = regular_user
    resp = await client.post("/api/invites", json={}, cookies={"lore_session": token})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_moderator_mint_binds_own_group(client, moderator_user, test_db):
    """A moderator's invite row carries their own uid as both inviter and group;
    the mint returns the raw token, persisted only as its sha256."""
    mod_uid, mod_token = moderator_user
    token = await _mint(client, mod_token)
    assert isinstance(token, str) and token
    invite = await _fetch_invite(test_db, token)
    assert invite is not None
    assert invite["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert invite["invited_by"] == mod_uid
    assert invite["moderator_id"] == mod_uid
    assert invite.get("used_at") is None


@pytest.mark.asyncio
async def test_admin_mint_takes_moderator_id_from_body(client, admin_user, moderator_user, regular_user, test_db):
    """Admin's invite: optional moderator_id from the body, validated as a moderator uid."""
    mod_uid, _ = moderator_user
    _, admin_token = admin_user
    token = await _mint(client, admin_token, {"moderator_id": mod_uid})
    assert (await _fetch_invite(test_db, token))["moderator_id"] == mod_uid

    token = await _mint(client, admin_token)
    # Surreal omits option fields holding NONE (step-1 ruling) — read with .get().
    assert (await _fetch_invite(test_db, token)).get("moderator_id") is None

    foreign_uid, _ = regular_user
    resp = await client.post(
        "/api/invites", json={"moderator_id": foreign_uid}, cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 422


# ─── Public register surface ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_get_shows_inviter_name(client, moderator_user):
    """GET surfaces the inviter's name for the register page; unknown token → 404."""
    _, mod_token = moderator_user
    token = await _mint(client, mod_token)
    resp = await client.get(f"/api/register/{token}")
    assert resp.status_code == 200
    assert resp.json() == {"inviter_name": "modinv"}

    resp = await client.get("/api/register/no-such-token")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_register_creates_bound_user_and_logs_in(client, admin_user, moderator_user, test_db):
    """Register creates role='user' bound to the invite's group + inviter and
    sets the session cookie — the new user lands logged in."""
    mod_uid, _ = moderator_user
    admin_uid, admin_token = admin_user
    token = await _mint(client, admin_token, {"moderator_id": mod_uid})

    resp = await client.post(
        f"/api/register/{token}",
        json={"name": "newcomer", "email": INVITE_EMAIL, "password": "pass123"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["role"] == "user"
    new_uid = data["user_id"]

    rows = await test_db.query(
        "SELECT role, created_by, moderator_id FROM type::record('users', $id)",
        {"id": new_uid},
    )
    assert rows[0]["role"] == "user"
    assert rows[0]["created_by"] == admin_uid
    assert rows[0]["moderator_id"] == mod_uid

    invite = await _fetch_invite(test_db, token)
    assert invite["used_at"] is not None
    assert invite["used_by"] == new_uid

    # Logged in: the cookie set by register authenticates /me.
    session = client.cookies.get("lore_session")
    assert session
    resp = await client.get("/api/auth/me", cookies={"lore_session": session})
    assert resp.status_code == 200
    assert resp.json()["email"] == INVITE_EMAIL
    assert resp.json()["can_manage_users"] is False


async def _fetch_group_pointer(test_db, uid: str) -> tuple:
    rows = await test_db.query(
        "SELECT created_by, moderator_id FROM type::record('users', $id)",
        {"id": uid},
    )
    # Surreal omits option fields holding NONE — read with .get().
    return rows[0].get("created_by"), rows[0].get("moderator_id")


@pytest.mark.asyncio
async def test_register_after_demoted_moderator_lands_ungrouped(client, admin_user, moderator_user, test_db):
    """An invite claimed after its moderator was demoted registers UNGROUPED —
    moderator_id must never point at a non-moderator row (schema ASSERT),
    while created_by still records the inviter."""
    mod_uid, mod_token = moderator_user
    _, admin_token = admin_user
    token = await _mint(client, mod_token)
    resp = await client.patch(
        f"/api/admin/users/{mod_uid}", json={"role": "user"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/register/{token}",
        json={"name": "orphan", "email": "ungrouped@test.com", "password": "pass123"},
    )
    assert resp.status_code == 200, resp.text
    created_by, moderator_id = await _fetch_group_pointer(test_db, resp.json()["user_id"])
    assert moderator_id is None
    assert created_by == mod_uid


@pytest.mark.asyncio
async def test_register_after_deleted_moderator_lands_ungrouped(client, admin_user, moderator_user, test_db):
    """Soft-deleted moderator at claim time: fetch_one reads None, the account
    lands ungrouped, created_by still records the inviter."""
    mod_uid, mod_token = moderator_user
    _, admin_token = admin_user
    token = await _mint(client, mod_token)
    resp = await client.delete(f"/api/admin/users/{mod_uid}", cookies={"lore_session": admin_token})
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/register/{token}",
        json={"name": "orphan2", "email": "ungrouped2@test.com", "password": "pass123"},
    )
    assert resp.status_code == 200, resp.text
    created_by, moderator_id = await _fetch_group_pointer(test_db, resp.json()["user_id"])
    assert moderator_id is None
    assert created_by == mod_uid


@pytest.mark.asyncio
async def test_register_second_use_404(client, moderator_user):
    """Single-use: after a successful register both GET and POST read 404."""
    _, mod_token = moderator_user
    token = await _mint(client, mod_token)
    resp = await client.post(
        f"/api/register/{token}",
        json={"name": "first", "email": INVITE_EMAIL, "password": "pass123"},
    )
    assert resp.status_code == 200
    resp = await client.get(f"/api/register/{token}")
    assert resp.status_code == 404
    resp = await client.post(
        f"/api/register/{token}",
        json={"name": "second", "email": "other@test.com", "password": "pass123"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_register_expired_404(client, admin_user, test_db):
    """An expired invite reads as invalid on both routes."""
    _, admin_token = admin_user
    token = "expired-token-value"
    await create_record("registration_invites", "test-expired-inv", {
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "invited_by": admin_user[0],
        "moderator_id": None,
        "expires_at": datetime.now(timezone.utc) - timedelta(hours=1),
    })
    resp = await client.get(f"/api/register/{token}")
    assert resp.status_code == 404
    resp = await client.post(
        f"/api/register/{token}",
        json={"name": "late", "email": INVITE_EMAIL, "password": "pass123"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_register_duplicate_email_409_does_not_burn_invite(client, admin_user, regular_user):
    """409 on a registered email leaves the invite reusable (checked before the claim)."""
    _, admin_token = admin_user
    token = await _mint(client, admin_token)
    resp = await client.post(
        f"/api/register/{token}",
        json={"name": "dup", "email": "user@test.com", "password": "pass123"},
    )
    assert resp.status_code == 409
    # The invite survived — the same token registers a fresh email.
    resp = await client.post(
        f"/api/register/{token}",
        json={"name": "ok", "email": INVITE_EMAIL, "password": "pass123"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_register_flushes_pending_invites(client, admin_user, project_with_doc, test_db):
    """Queued project invites for the email are applied at registration
    (same flush as create_user_admin)."""
    admin_uid, admin_token = admin_user
    pid = project_with_doc[0]
    await create_record("pending_invites", "test-pending-reg", {
        "email": INVITE_EMAIL,
        "project_id": pid,
        "access_level": "readonly",
        "invited_by": admin_uid,
    })
    token = await _mint(client, admin_token)
    resp = await client.post(
        f"/api/register/{token}",
        json={"name": "flushed", "email": INVITE_EMAIL, "password": "pass123"},
    )
    assert resp.status_code == 200
    new_uid = resp.json()["user_id"]
    members = await test_db.query(
        "SELECT access_level FROM project_members WHERE user_id = $uid AND project_id = $pid",
        {"uid": new_uid, "pid": pid},
    )
    assert members and members[0]["access_level"] == "readonly"
    leftover = await test_db.query(
        "SELECT id FROM pending_invites WHERE email = $e", {"e": INVITE_EMAIL},
    )
    assert not leftover
