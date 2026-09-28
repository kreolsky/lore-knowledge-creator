"""Integration tests for cabinet (user profile) routes."""

import jwt
import pytest

from config import ALGORITHM, SECRET_KEY

# ─── Change Name ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_change_name(client, regular_user):
    uid, token = regular_user
    resp = await client.patch(
        "/api/cabinet/name",
        json={"name": "NewName"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "NewName"
    # JWT cookie should be re-issued with new name
    new_token = resp.cookies.get("lore_session")
    assert new_token is not None
    payload = jwt.decode(new_token, SECRET_KEY, algorithms=[ALGORITHM])
    assert payload["name"] == "NewName"


@pytest.mark.asyncio
async def test_change_name_unauthenticated(client):
    resp = await client.patch("/api/cabinet/name", json={"name": "X"})
    assert resp.status_code == 401


# ─── Change Email ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_change_email(client, regular_user):
    uid, token = regular_user
    resp = await client.patch(
        "/api/cabinet/email",
        json={"email": "newemail@test.com", "current_password": "userpass"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "newemail@test.com"
    # JWT cookie re-issued with new email
    new_token = resp.cookies.get("lore_session")
    assert new_token is not None
    payload = jwt.decode(new_token, SECRET_KEY, algorithms=[ALGORITHM])
    assert payload["email"] == "newemail@test.com"


@pytest.mark.asyncio
async def test_change_email_wrong_password(client, regular_user):
    _, token = regular_user
    resp = await client.patch(
        "/api/cabinet/email",
        json={"email": "x@test.com", "current_password": "wrongpass"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_change_email_duplicate(client, admin_user, regular_user):
    """Cannot change email to one already used by another user."""
    _, token = regular_user
    resp = await client.patch(
        "/api/cabinet/email",
        json={"email": "admin@test.com", "current_password": "userpass"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 409


# ─── Change Password ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_change_password(client, regular_user):
    uid, token = regular_user
    resp = await client.patch(
        "/api/cabinet/password",
        json={"current_password": "userpass", "new_password": "newpass123"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    # Verify new password works for login
    resp2 = await client.post(
        "/api/auth/login",
        json={"email": "user@test.com", "password": "newpass123"},
    )
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_change_password_wrong_current(client, regular_user):
    _, token = regular_user
    resp = await client.patch(
        "/api/cabinet/password",
        json={"current_password": "wrong", "new_password": "newpass"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 403


# ─── PIN Management ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_pin(client, regular_user):
    _, token = regular_user
    resp = await client.patch(
        "/api/cabinet/pin",
        json={"pin": "1234"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["has_pin"] is True


@pytest.mark.asyncio
async def test_set_pin_invalid_format(client, regular_user):
    """PIN must be exactly 4 digits."""
    _, token = regular_user
    resp = await client.patch(
        "/api/cabinet/pin",
        json={"pin": "12"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_set_pin_non_numeric(client, regular_user):
    _, token = regular_user
    resp = await client.patch(
        "/api/cabinet/pin",
        json={"pin": "abcd"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_remove_pin(client, regular_user):
    _, token = regular_user
    # Set PIN first
    await client.patch(
        "/api/cabinet/pin",
        json={"pin": "1234"},
        cookies={"lore_session": token},
    )
    # Remove PIN
    resp = await client.patch(
        "/api/cabinet/pin",
        json={"pin": None},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["has_pin"] is False


# ─── Verify PIN ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_pin_correct(client, regular_user):
    _, token = regular_user
    # Set PIN first
    await client.patch(
        "/api/cabinet/pin",
        json={"pin": "5678"},
        cookies={"lore_session": token},
    )
    resp = await client.post(
        "/api/cabinet/verify-pin",
        json={"pin": "5678"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


@pytest.mark.asyncio
async def test_verify_pin_wrong(client, regular_user):
    _, token = regular_user
    # Set PIN first
    await client.patch(
        "/api/cabinet/pin",
        json={"pin": "5678"},
        cookies={"lore_session": token},
    )
    resp = await client.post(
        "/api/cabinet/verify-pin",
        json={"pin": "0000"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is False


@pytest.mark.asyncio
async def test_verify_pin_no_pin_set(client, regular_user):
    """Verify-pin when no PIN is set should return valid=False."""
    _, token = regular_user
    resp = await client.post(
        "/api/cabinet/verify-pin",
        json={"pin": "1234"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is False


# ─── Rate Limiting ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_pin_rate_limit(client, regular_user):
    """Verify-pin should be rate-limited after too many wrong attempts."""
    _, token = regular_user
    # Set PIN first
    await client.patch(
        "/api/cabinet/pin",
        json={"pin": "9999"},
        cookies={"lore_session": token},
    )
    # Exhaust rate limit with wrong PINs
    for _ in range(5):
        await client.post(
            "/api/cabinet/verify-pin",
            json={"pin": "0000"},
            cookies={"lore_session": token},
        )
    # 6th attempt should be rate-limited
    resp = await client.post(
        "/api/cabinet/verify-pin",
        json={"pin": "0000"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 429
