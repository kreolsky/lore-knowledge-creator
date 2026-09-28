"""Security-specific tests — JWT expiration, WS auth, token validation."""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from config import ALGORITHM, SECRET_KEY


def _make_token_raw(**claims: object) -> str:
    """Encode a JWT with arbitrary claims (no defaults)."""
    return jwt.encode(dict(claims), SECRET_KEY, algorithm=ALGORITHM)


# ── C-3: JWT expiration enforcement ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_token_without_exp(client):
    """Token missing the exp claim must be rejected."""
    token = _make_token_raw(user_id="u1", name="x", email="x@t.com", role="user")
    resp = await client.get("/api/auth/me", cookies={"lore_session": token})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_reject_expired_token(client):
    """Token with exp in the past must be rejected."""
    token = _make_token_raw(
        user_id="u1", name="x", email="x@t.com", role="user",
        exp=datetime.now(timezone.utc) - timedelta(seconds=10),
        iat=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    resp = await client.get("/api/auth/me", cookies={"lore_session": token})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_accept_valid_token_with_exp(client, admin_user):
    """Token with valid exp claim must be accepted."""
    _, token = admin_user
    resp = await client.get("/api/auth/me", cookies={"lore_session": token})
    assert resp.status_code == 200


# ── C-3: Login issues JWT with exp ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_jwt_contains_exp(client, admin_user):
    """After login, the JWT cookie must contain exp and iat claims."""
    resp = await client.post("/api/auth/login", json={"email": "admin@test.com", "password": "adminpass"})
    assert resp.status_code == 200
    token = resp.cookies.get("lore_session")
    assert token
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    assert "exp" in payload
    assert "iat" in payload
    assert payload["exp"] > payload["iat"]
