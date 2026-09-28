"""Integration tests for user routes."""

import pytest


@pytest.mark.asyncio
async def test_list_users_admin(client, admin_user):
    _, token = admin_user
    resp = await client.get("/api/admin/users", cookies={"lore_session": token})
    assert resp.status_code == 200
    users = resp.json()
    assert any(u["name"] == "testadmin" for u in users)


@pytest.mark.asyncio
async def test_list_users_non_admin(client, regular_user):
    _, token = regular_user
    resp = await client.get("/api/admin/users", cookies={"lore_session": token})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_user(client, admin_user):
    _, token = admin_user
    resp = await client.post(
        "/api/admin/users",
        json={"name": "newuser", "email": "new@test.com", "password": "pass123", "role": "user"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "newuser"
    assert data["email"] == "new@test.com"


@pytest.mark.asyncio
async def test_create_user_duplicate_email(client, admin_user):
    """Cannot create user with email already in use."""
    _, token = admin_user
    resp = await client.post(
        "/api/admin/users",
        json={"name": "dup", "email": "admin@test.com", "password": "pass123", "role": "user"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_update_user_facts(client, regular_user):
    uid, token = regular_user
    resp = await client.put(
        f"/api/users/{uid}",
        json={"user_facts": "I like tests"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["user_facts"] == "I like tests"


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
async def test_delete_user(client, admin_user):
    _, token = admin_user
    # Create a user to delete
    resp = await client.post(
        "/api/admin/users",
        json={"name": "todelete", "email": "todelete@test.com", "password": "pass123", "role": "user"},
        cookies={"lore_session": token},
    )
    uid = resp.json()["user_id"]
    resp = await client.delete(f"/api/admin/users/{uid}", cookies={"lore_session": token})
    assert resp.status_code == 200
    assert resp.json()["success"] is True


@pytest.mark.asyncio
async def test_delete_releases_email_for_recreation(client, admin_user):
    """Soft-delete frees the email: idx_users_email is UNIQUE over ALL rows
    (the index does not see deleted_at), so re-creating a soft-deleted user's
    email must not 500 on the index — the duplicate 409 check only reads
    live rows and lets the insert through to it."""
    _, token = admin_user
    body = {"name": "recycled", "email": "recycled@test.com", "password": "pass123", "role": "user"}
    resp = await client.post("/api/admin/users", json=body, cookies={"lore_session": token})
    assert resp.status_code == 200
    uid = resp.json()["user_id"]
    resp = await client.delete(f"/api/admin/users/{uid}", cookies={"lore_session": token})
    assert resp.status_code == 200
    resp = await client.post("/api/admin/users", json=body, cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == "recycled@test.com"


@pytest.mark.asyncio
async def test_delete_self(client, admin_user):
    uid, token = admin_user
    resp = await client.delete(f"/api/admin/users/{uid}", cookies={"lore_session": token})
    assert resp.status_code == 400


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
async def test_patch_user_admin(client, admin_user, regular_user):
    _, admin_token = admin_user
    user_uid, _ = regular_user
    resp = await client.patch(
        f"/api/admin/users/{user_uid}",
        json={"name": "renamed_user"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed_user"


@pytest.mark.asyncio
async def test_patch_user_admin_email(client, admin_user, regular_user):
    """Admin can change user email."""
    _, admin_token = admin_user
    user_uid, _ = regular_user
    resp = await client.patch(
        f"/api/admin/users/{user_uid}",
        json={"email": "changed@test.com"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    assert resp.json()["email"] == "changed@test.com"


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


@pytest.mark.asyncio
async def test_patch_user_admin_empty_body(client, admin_user, regular_user):
    _, admin_token = admin_user
    user_uid, _ = regular_user
    resp = await client.patch(
        f"/api/admin/users/{user_uid}",
        json={},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 400
