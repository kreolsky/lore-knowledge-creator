"""Tests for backend audit batch 3 — S-8 (updated_at), P-7 (rate limiter cleanup)."""


import pytest

# ─── S-8: updated_at set on UPDATE queries ────────────────────────────────────


@pytest.mark.asyncio
async def test_update_user_facts_sets_updated_at(client, admin_user):
    """PUT /api/users/{id} should set updated_at on user_facts change."""
    uid, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.put(f"/api/users/{uid}", json={"user_facts": "Some facts"}, cookies=cookies)
    assert resp.status_code == 200
    updated_at = resp.json().get("updated_at")
    assert updated_at is not None
