"""Tests for backend audit round 2 — sort safety, parallel dispatch, config constants,
transactions, token_version, session limits, pagination, notes DRY, response format, logging."""


import pytest

# ─── F-7: JWT token_version invalidation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_token_version_mismatch_rejects_request(client, admin_user):
    """A JWT with stale token_version must be rejected with 401."""
    from helpers import make_token as _mt

    from db import create_record, get_db
    uid = "tvtest001"
    db = await get_db()
    await db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "tokenveruser",
        "email": "tv@test.com",
        "password_hash": "unused",
        "role": "user",
        "token_version": 0,
    })
    # Create token with token_version=0
    token = _mt(uid, "tokenveruser", "user", "tv@test.com", token_version=0)
    # Verify token works initially
    resp = await client.get("/api/projects", cookies={"lore_session": token})
    assert resp.status_code == 200

    # Bump token_version via the canonical helper (simulates password change /
    # forced logout) — updates the DB AND invalidates the auth cache, the path a
    # real session-invalidation site must use.
    from auth import bump_token_version
    await bump_token_version(uid)
    # Same token should now be rejected
    resp = await client.get("/api/projects", cookies={"lore_session": token})
    assert resp.status_code == 401, f"Expected 401 after token_version bump, got {resp.status_code}"


# ─── F-9: list_projects pagination ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_projects_supports_pagination(client, admin_user, test_db):
    """GET /api/projects must accept limit and offset query params."""
    _, token = admin_user
    # Create 3 projects
    pids = []
    for i in range(3):
        resp = await client.post(
            "/api/projects",
            json={"name": f"PagProject {i}"},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        pids.append(resp.json()["project_id"])

    # Fetch with limit=2
    resp = await client.get(
        "/api/projects?limit=2",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    # Fetch with offset=2 — should get 1 remaining
    resp = await client.get(
        "/api/projects?limit=10&offset=2",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ─── F-11: Response format {"ok": True} → {"success": True} ──────────────────


@pytest.mark.asyncio
async def test_delete_session_returns_success_key(client, admin_user, project_with_doc):
    """DELETE /chat/sessions/{id} must return {"success": true}, not {"ok": true}."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    assert resp.status_code in (200, 201)
    sid = resp.json()["session_id"]
    # Delete it
    resp = await client.delete(
        f"/api/chat/sessions/{sid}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "success" in body, f"Expected 'success' key, got {body}"
    assert "ok" not in body, f"'ok' key should be replaced with 'success', got {body}"


@pytest.mark.asyncio
async def test_delete_message_branch_returns_success_key(client, admin_user, project_with_doc):
    """DELETE /chat/.../messages/{id} must return {"success": true}, not {"ok": true}
    (a NOTE chat: AI chats have no message delete since branch sessions)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "is_note": True},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "hello"},
        cookies={"lore_session": token},
    )
    assert resp.status_code in (200, 201)
    mid = resp.json()["message_id"]
    # Delete message branch
    resp = await client.delete(
        f"/api/chat/messages/{mid}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "success" in body, f"Expected 'success' key, got {body}"
    assert "ok" not in body, f"'ok' key should be replaced with 'success', got {body}"


# ─── F-12: Logging verbosity ─────────────────────────────────────────────────


def test_collab_ws_connect_uses_debug_level():
    """WS connect/disconnect in collab_project_ws.py should use DEBUG, not INFO.

    (The per-entity twin inspected the deleted routes.collab route; the one
    remaining collab channel is routes/collab_project_ws.py — plan fewer-layers.)"""
    import inspect

    from routes import collab_project_ws

    source = inspect.getsource(collab_project_ws)
    # The "Project collab WS connect:" and "disconnect:" log lines should use logger.debug
    assert 'logger.info("Project collab WS connect:' not in source, \
        "collab_project_ws.py still uses logger.info for WS connect — should be logger.debug"
    assert 'logger.info("Project collab WS disconnect:' not in source, \
        "collab_project_ws.py still uses logger.info for WS disconnect — should be logger.debug"


def test_project_ws_connect_uses_debug_level():
    """WS connect in project_ws.py should use DEBUG, not INFO."""
    import inspect

    from routes import project_ws

    source = inspect.getsource(project_ws)
    assert 'logger.info("Project WS connect:' not in source, \
        "project_ws.py still uses logger.info for WS connect — should be logger.debug"
