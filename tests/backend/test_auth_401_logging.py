"""TDD for reason-tagged auth-401 logging in get_current_user.

Each auth-401 raise site must emit a log_security event distinguishing the reason
(no_cookie / expired / invalid / user_not_found) and the request path, so prod can
diagnose spontaneous-logouts from the structured security log alone.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jwt
import pytest

import auth as auth_module
from config import ALGORITHM, SECRET_KEY


def _make_request_stub(path: str = "/api/documents/abc"):
    """Build a stand-in Request exposing .url.path."""
    return SimpleNamespace(url=SimpleNamespace(path=path))


async def _call(lore_session, payload_user=None):
    """Invoke get_current_user with a Request stub and optional DB user record.

    _verify_token_version does a local `from db import fetch_one`, so patch db.fetch_one.
    """
    req = _make_request_stub()
    with patch("db.fetch_one", new=AsyncMock(return_value=payload_user)):
        with patch("auth.log_security") as logged:
            with pytest.raises(Exception):
                await auth_module.get_current_user(lore_session=lore_session, request=req)
            return logged


async def test_no_cookie_logs_no_cookie_with_path():
    logged = await _call(None)
    logged.assert_any_call("no_cookie", user_id=None, path="/api/documents/abc")


async def test_expired_token_logs_expired_reason():
    expired = jwt.encode(
        {"user_id": "u1", "token_version": 0, "exp": 1},
        SECRET_KEY, algorithm=ALGORITHM,
    )
    logged = await _call(expired)
    logged.assert_any_call("expired", user_id="u1", path="/api/documents/abc")


async def test_invalid_token_logs_invalid_reason():
    logged = await _call("not.a.jwt")
    logged.assert_any_call("invalid", user_id=None, path="/api/documents/abc")


async def test_user_not_found_logs_user_not_found_with_id():
    # Valid signature but user record absent in DB → _verify_token_version raises 401.
    payload = {"user_id": "ghost", "token_version": 0,
               "exp": 2_000_000_000}
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    logged = await _call(token, payload_user=None)
    logged.assert_any_call("user_not_found", user_id="ghost", path="/api/documents/abc")
