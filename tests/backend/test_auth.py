"""Integration tests for auth routes."""

import pytest


@pytest.mark.asyncio
async def test_login_success(client, admin_user):
    resp = await client.post("/api/auth/login", json={"email": "admin@test.com", "password": "adminpass"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "testadmin"
    assert data["email"] == "admin@test.com"
    assert data["role"] == "admin"
    assert "lore_session" in resp.cookies


@pytest.mark.asyncio
async def test_login_wrong_password(client, admin_user):
    resp = await client.post("/api/auth/login", json={"email": "admin@test.com", "password": "wrong"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_nonexistent_email(client):
    resp = await client.post("/api/auth/login", json={"email": "ghost@test.com", "password": "nope"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_logout(client, admin_user):
    resp = await client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


@pytest.mark.asyncio
async def test_me_authenticated(client, admin_user):
    _, token = admin_user
    resp = await client.get("/api/auth/me", cookies={"lore_session": token})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "testadmin"
    assert data["email"] == "admin@test.com"
    assert data["role"] == "admin"
    assert data["has_pin"] is False


@pytest.mark.asyncio
async def test_me_unauthenticated(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_has_pin_true(client, admin_user):
    """When user has pin_hash set, /me returns has_pin=True."""
    _, token = admin_user
    # Set PIN via cabinet endpoint
    resp = await client.patch(
        "/api/cabinet/pin",
        json={"pin": "1234"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    resp = await client.get("/api/auth/me", cookies={"lore_session": token})
    assert resp.status_code == 200
    assert resp.json()["has_pin"] is True
