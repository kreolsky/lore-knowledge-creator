"""Tests for user preferences endpoints (per-project and global)."""

from __future__ import annotations


async def test_prefs_401_without_auth(client):
    """Both global and project endpoints reject unauthenticated requests."""
    resp = await client.get("/api/preferences/_global")
    assert resp.status_code == 401

    resp = await client.put(
        "/api/preferences/_global",
        json={"preferences": {"theme": "dark"}},
    )
    assert resp.status_code == 401

    resp = await client.get("/api/preferences/some-project-id")
    assert resp.status_code == 401
