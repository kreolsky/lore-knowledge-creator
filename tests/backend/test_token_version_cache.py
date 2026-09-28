"""Tests for the token_version TTL cache in auth._verify_token_version."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest

import auth
from auth import (
    _invalidate_token_version_cache,
    _verify_token_version,
    validate_ws_token,
)
from config import ALGORITHM, SECRET_KEY


@pytest.fixture(autouse=True)
def _clear_cache():
    auth._token_version_cache.clear()
    yield
    auth._token_version_cache.clear()


@pytest.mark.asyncio
async def test_second_call_hits_cache_not_db(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch_one(table, uid):
        calls["n"] += 1
        return {"token_version": 3}

    monkeypatch.setattr("db.fetch_one", fake_fetch_one)
    payload = {"user_id": "u1", "token_version": 3}

    await _verify_token_version(payload)  # miss -> DB
    await _verify_token_version(payload)  # hit -> cache
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_stale_jwt_rejected_from_cache(monkeypatch):
    async def fake_fetch_one(table, uid):
        return {"token_version": 5}

    monkeypatch.setattr("db.fetch_one", fake_fetch_one)

    # Prime the cache with the current DB version.
    await _verify_token_version({"user_id": "u2", "token_version": 5})

    # A token carrying an outdated version must still be rejected from cache.
    with pytest.raises(Exception) as exc:
        await _verify_token_version({"user_id": "u2", "token_version": 4})
    assert getattr(exc.value, "status_code", None) == 401


@pytest.mark.asyncio
async def test_expired_cache_refetches(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch_one(table, uid):
        calls["n"] += 1
        return {"token_version": 1}

    monkeypatch.setattr("db.fetch_one", fake_fetch_one)
    payload = {"user_id": "u3", "token_version": 1}

    await _verify_token_version(payload)
    # Force expiry by rewriting the cached entry's deadline into the past.
    tv, role, _ = auth._token_version_cache["u3"]
    auth._token_version_cache["u3"] = (tv, role, 0.0)
    await _verify_token_version(payload)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_invalidate_evicts_entry(monkeypatch):
    async def fake_fetch_one(table, uid):
        return {"token_version": 7}

    monkeypatch.setattr("db.fetch_one", fake_fetch_one)
    await _verify_token_version({"user_id": "u4", "token_version": 7})
    assert "u4" in auth._token_version_cache
    _invalidate_token_version_cache("u4")
    assert "u4" not in auth._token_version_cache


@pytest.mark.asyncio
async def test_validate_ws_token_overlays_db_role(monkeypatch):
    """WS connect carries the fresh DB role, not the JWT's stale claim.

    A demoted admin's cookie keeps role=admin for up to 14 days; without the
    overlay a NEW collab-WS connect authorizes with the stale claim while
    REST already refuses (get_current_user overlay).
    """
    async def fake_fetch_one(table, uid):
        return {"token_version": 0, "role": "user"}

    monkeypatch.setattr("db.fetch_one", fake_fetch_one)
    token = jwt.encode(
        {
            "user_id": "u5", "role": "admin", "token_version": 0,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        SECRET_KEY, algorithm=ALGORITHM,
    )
    ws = SimpleNamespace(
        cookies={"lore_session": token},
        url=SimpleNamespace(path="/ws/collab/project/p1"),
    )
    payload = await validate_ws_token(ws)
    assert payload is not None
    assert payload["role"] == "user"
