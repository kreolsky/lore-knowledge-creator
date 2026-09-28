"""Tests for backend audit fixes — cycle protection, 404 guards, DB-level pagination."""

import pytest

# ─── H-3: 404 guard on PATCH /api/checkpoints/{checkpoint_id} ────────────────


@pytest.mark.asyncio
async def test_patch_nonexistent_checkpoint_returns_404(client, admin_user):
    """PATCH on a non-existent checkpoint must return 404, not silently succeed."""
    _, token = admin_user
    resp = await client.patch(
        "/api/checkpoints/nonexistent-cp-id",
        json={"label": "new-label"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


# ─── M-4: DB-level pagination for unbounded refs query ───────────────────────


# ─── token_version: null cookie reissue bug ─────────────────────────────────


@pytest.mark.asyncio
async def test_db_rejects_null_token_version(admin_user):
    """The schema (token_version TYPE int DEFAULT 0) + surrealdb 2.0.0 strict coercion
    must guarantee token_version is never NULL — writing NULL is rejected at the DB.

    Pre-2.0.0 the SDK swallowed the coerce error (write silently lost), so a legacy NULL
    could linger; now it is structurally impossible. This pins that guarantee.
    """
    uid, _ = admin_user
    from surrealdb.errors import SurrealError

    from db import get_db
    db = await get_db()
    with pytest.raises(SurrealError):
        await db.query(
            "UPDATE type::record('users', $id) SET token_version = NULL",
            {"id": uid},
        )


@pytest.mark.asyncio
async def test_me_sliding_cookie_preserves_token_version(client, admin_user):
    """Sliding-window cookie from /me must carry a valid token_version and keep working."""
    uid, token = admin_user
    # /me should succeed and re-issue a valid cookie
    resp = await client.get("/api/auth/me", cookies={"lore_session": token})
    assert resp.status_code == 200
    # Extract re-issued cookie and verify it works for subsequent requests
    new_cookie = resp.cookies.get("lore_session")
    assert new_cookie is not None
    # The re-issued cookie must also work (not rejected as legacy)
    resp2 = await client.get("/api/auth/me", cookies={"lore_session": new_cookie})
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_login_produces_full_lifecycle_session(client):
    """Login must produce a valid full-lifecycle session. token_version defaults to 0
    (schema: int DEFAULT 0) and is always a valid int through the cookie lifecycle."""
    from password import hash_secret

    from db import create_record, get_db
    db = await get_db()
    uid = "test-null-tv-user"
    # type::record() — bare hyphenated id parses as arithmetic on surrealdb 2.0.0.
    await db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "nulltv", "email": "nulltv@test.com",
        "password_hash": hash_secret("pass"), "role": "user", "user_facts": "",
    })
    # Login
    resp = await client.post("/api/auth/login", json={"email": "nulltv@test.com", "password": "pass"})
    assert resp.status_code == 200
    cookie = resp.cookies.get("lore_session")
    # Full lifecycle: /me → re-issued cookie → /me again
    resp2 = await client.get("/api/auth/me", cookies={"lore_session": cookie})
    assert resp2.status_code == 200
    new_cookie = resp2.cookies.get("lore_session")
    resp3 = await client.get("/api/auth/me", cookies={"lore_session": new_cookie})
    assert resp3.status_code == 200
    # Cleanup
    await db.query("DELETE type::record('users', $id)", {"id": uid})


