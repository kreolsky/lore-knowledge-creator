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


def _session_set_cookie(resp) -> str:
    """The raw Set-Cookie header of the session cookie (attributes included)."""
    headers = [h for h in resp.headers.get_list("set-cookie") if h.startswith("lore_session=")]
    assert len(headers) == 1, resp.headers.get_list("set-cookie")
    return headers[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("forwarded", "secure"), [
    (None, False),              # plain HTTP, no proxy (http://localhost:8080)
    ("http", False),
    ("https", True),            # TLS terminated by the operator's proxy
    ("https, http", True),      # proxy chain: the first hop is the browser's scheme
])
async def test_login_cookie_secure_follows_browser_scheme(client, admin_user, forwarded, secure):
    """The Secure flag is derived from the scheme the browser used — no setting.

    A Secure cookie over plain HTTP never comes back (sign-in does not stick);
    a non-Secure one over HTTPS can leak on a downgrade. Neither may depend on
    an operator finding an env var."""
    headers = {"X-Forwarded-Proto": forwarded} if forwarded else {}
    resp = await client.post(
        "/api/auth/login", json={"email": "admin@test.com", "password": "adminpass"}, headers=headers,
    )
    assert resp.status_code == 200
    attrs = [a.strip().lower() for a in _session_set_cookie(resp).split(";")]
    assert ("secure" in attrs) is secure, attrs


@pytest.mark.asyncio
@pytest.mark.parametrize(("forwarded", "secure"), [(None, False), ("https", True)])
async def test_login_sets_device_cookie(client, admin_user, forwarded, secure):
    """A successful login marks the browser as a known device for that account."""
    headers = {"X-Forwarded-Proto": forwarded} if forwarded else {}
    resp = await client.post(
        "/api/auth/login", json={"email": "admin@test.com", "password": "adminpass"}, headers=headers,
    )
    assert resp.status_code == 200
    headers = [h for h in resp.headers.get_list("set-cookie") if h.startswith("lore_device=")]
    assert len(headers) == 1, resp.headers.get_list("set-cookie")
    attrs = [a.strip().lower() for a in headers[0].split(";")]
    assert "httponly" in attrs and "path=/api/auth" in attrs and "samesite=lax" in attrs, attrs
    assert ("secure" in attrs) is secure, attrs


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
