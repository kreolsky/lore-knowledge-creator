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
