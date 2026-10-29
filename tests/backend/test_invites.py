"""Registration invite links — mint binding, public register, single-use, TTL.

POST /api/invites
(admin | moderator) mints a single-use 3-day token bound to a moderator's
group; GET/POST /api/register/{token} are public, ride the login rate limiter,
and a successful POST creates a role='user' account (created_by = inviter,
moderator_id = the invite's binding), flushes pending project invites for the
email, and lands the new user logged in via the session cookie.
"""

import hashlib

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
