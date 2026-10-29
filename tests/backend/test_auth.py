"""Integration tests for auth routes."""

import pytest


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
async def test_me_unauthenticated(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401
