"""Tests for AI Chat routes — sessions CRUD, messages, access control, system prompt injection."""


# ─── Retired proposal render tail ─────────────────────────────────────────────


async def test_historical_tool_proposal_rows_do_not_surface(test_db):
    """The proposal-card render cluster is retired (plan
    retire-the-proposal-card-render-cluster): historical `tool_proposals` rows no
    longer surface as a `proposals[]` array on serialized messages — old threads
    render their tool calls through the agent_steps chip fallback instead, and a
    payload the deleted card union would have rejected changes nothing."""
    from routes.chat.messages import load_session_messages

    from db import get_timed_proxy

    await test_db.query(
        "CREATE type::record('messages', $mid) SET chat_id = 'c1', role = 'assistant', "
        "content = ''",
        {"mid": "retired-1"},
    )
    await test_db.query(
        "CREATE type::record('tool_proposals', $id) SET message_id = $mid, "
        "tool_call_id = 'tc-1', kind = 'agent_edit', status = 'applied', "
        "payload = { tool_call_id: 'tc-1', kind: 'edit', doc_id: 'd1', "
        "old_string: 'a', new_string: 'b' }",
        {"id": "retired-row-1", "mid": "retired-1"},
    )
    rows = await load_session_messages(
        get_timed_proxy(test_db),
        {"session_id": "c1", "user_id": "u-1"},
        "c1",
    )
    assert rows, "the message must still serialize"
    assert rows[0]["message_id"] == "retired-1"
    assert "proposals" not in rows[0]


# ─── Access control ───────────────────────────────────────────────────────────


async def test_session_access_denied_other_user(client, admin_user, regular_user, project_with_doc):
    """Regular user cannot access admin's chat session."""
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    _, user_token = regular_user

    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": admin_token},
    )
    sid = resp.json()["session_id"]

    # Regular user tries to access — should get 404 (ownership check)
    resp = await client.get(
        f"/api/chat/sessions/{sid}/messages",
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 404


async def test_unauthenticated_request(client):
    """No cookie → 401."""
    resp = await client.get("/api/chat/sessions?project_id=any")
    assert resp.status_code == 401


# ─── Models endpoint ──────────────────────────────────────────────────────────


async def test_models_returns_list(client, admin_user):
    """GET /api/chat/models returns a list (may be empty if API unreachable)."""
    _, token = admin_user
    resp = await client.get(
        "/api/chat/models",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data
    assert isinstance(data["models"], list)
