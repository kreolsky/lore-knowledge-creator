"""Integration tests for user routes."""

import pytest


@pytest.mark.asyncio
async def test_list_users_non_admin(client, regular_user):
    _, token = regular_user
    resp = await client.get("/api/admin/users", cookies={"lore_session": token})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_user_facts_other_user(client, admin_user, regular_user):
    """Cannot update another user's facts — IDOR protection."""
    admin_uid, _ = admin_user
    _, user_token = regular_user
    resp = await client.put(
        f"/api/users/{admin_uid}",
        json={"user_facts": "hacked"},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_user_project_access(client, admin_user, regular_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    user_uid, _ = regular_user
    # Grant access first
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "readonly"},
        cookies={"lore_session": admin_token},
    )
    resp = await client.get(
        f"/api/admin/users/{user_uid}/project-access",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    access_map = resp.json()
    assert access_map[pid] == "readonly"


@pytest.mark.asyncio
async def test_get_user_project_access_non_admin(client, regular_user):
    uid, token = regular_user
    resp = await client.get(
        f"/api/admin/users/{uid}/project-access",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_patch_user_admin_email_duplicate(client, admin_user, regular_user):
    """Admin cannot set email to one already used by another user."""
    _, admin_token = admin_user
    user_uid, _ = regular_user
    resp = await client.patch(
        f"/api/admin/users/{user_uid}",
        json={"email": "admin@test.com"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 409
