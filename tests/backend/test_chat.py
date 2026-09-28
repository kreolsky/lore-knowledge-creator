"""Tests for AI Chat routes — sessions CRUD, messages, access control, system prompt injection."""

import re
from contextlib import ExitStack, asynccontextmanager
from unittest.mock import patch

import pytest
from helpers import pin_chat_api, pinned_chat_api

# The per-turn send stamp rides the tail of every outbound `prompt` (UTC in
# tests — users carry no timezone); assertions pin the stable part. Full stamp
# suite: test_prepare_agent_turn.py.
SENT_STAMP_RE = re.compile(
    r"\[sent \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\]$"
)


def _assert_prompt_text(prompt, expected: str) -> None:
    """The outbound prompt is the user text + time stamps (anchor may ride the
    middle on root turns) — assert the stable leading part + the send stamp."""
    assert prompt.startswith(expected), prompt
    assert SENT_STAMP_RE.search(prompt), prompt

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


# ─── Session CRUD ─────────────────────────────────────────────────────────────


async def test_create_session(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test-model"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["session_id"]
    assert data["project_id"] == pid
    assert data["model"] == "test-model"
    assert data["title"] == ""


async def test_list_sessions(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "m1"},
        cookies={"lore_session": token},
    )
    await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "m2"},
        cookies={"lore_session": token},
    )
    resp = await client.get(
        f"/api/chat/sessions?project_id={pid}&document_id={doc_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    sessions = resp.json()
    assert len(sessions) == 2
    assert sessions[0]["model"] == "m2"


async def test_update_session(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]
    resp = await client.patch(
        f"/api/chat/sessions/{sid}",
        json={"title": "My Chat", "model": "gpt-4"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "My Chat"
    assert resp.json()["model"] == "gpt-4"


async def test_delete_session(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]
    resp = await client.delete(
        f"/api/chat/sessions/{sid}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    resp = await client.get(
        f"/api/chat/sessions?project_id={pid}&document_id={doc_id}",
        cookies={"lore_session": token},
    )
    assert all(s["session_id"] != sid for s in resp.json())


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


async def test_session_requires_project_access(client, regular_user):
    """Cannot create session in non-existent project."""
    _, token = regular_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": "nonexistent-project", "document_id": "any-doc"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


async def test_unauthenticated_request(client):
    """No cookie → 401."""
    resp = await client.get("/api/chat/sessions?project_id=any")
    assert resp.status_code == 401


# ─── Messages ─────────────────────────────────────────────────────────────────


async def test_create_and_list_messages(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]
    # Create user message
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "Hello AI"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201
    user_msg = resp.json()
    assert user_msg["role"] == "user"
    assert user_msg["content"] == "Hello AI"
    assert user_msg.get("parent_id") is None

    # Create assistant message as child
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "assistant", "content": "Hi there!", "parent_id": user_msg["message_id"]},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201
    assistant_msg = resp.json()
    assert assistant_msg["parent_id"] == user_msg["message_id"]

    # List all messages
    resp = await client.get(
        f"/api/chat/sessions/{sid}/messages",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    messages = resp.json()
    assert len(messages) == 2


async def test_message_tree_fork(client, admin_user, project_with_doc):
    """Fork creates a sibling message with the same parent_id.

    H3: this endpoint only ever creates USER messages now (assistant rows are made
    solely by the completion stream), so the fork is exercised with sibling user
    messages under a shared parent — the tree structure, not the role, is the point.
    """
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]
    # Root message
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"content": "Original question"},
        cookies={"lore_session": token},
    )
    root_id = resp.json()["message_id"]

    # Two sibling children sharing the same parent (a fork).
    child_ids = []
    for content in ("Follow-up 1", "Follow-up 2"):
        r = await client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"content": content, "parent_id": root_id},
            cookies={"lore_session": token},
        )
        assert r.status_code == 201, r.text
        # H3: role is forced to 'user' regardless of what the client sends.
        assert r.json()["role"] == "user"
        child_ids.append(r.json()["message_id"])

    resp = await client.get(
        f"/api/chat/sessions/{sid}/messages",
        cookies={"lore_session": token},
    )
    messages = resp.json()
    assert len(messages) == 3
    # Both children are siblings under root.
    children = [m for m in messages if m["message_id"] in child_ids]
    assert len(children) == 2
    assert children[0]["parent_id"] == children[1]["parent_id"] == root_id


async def test_edit_message(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "Typo message"},
        cookies={"lore_session": token},
    )
    mid = resp.json()["message_id"]
    resp = await client.patch(
        f"/api/chat/messages/{mid}",
        json={"content": "Fixed message"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "Fixed message"


# ─── Delete message branch ───────────────────────────────────────────────────


async def test_delete_message_single(client, admin_user, project_with_doc):
    """Delete a leaf message — only that message is soft-deleted."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "To be deleted"},
        cookies={"lore_session": token},
    )
    mid = resp.json()["message_id"]

    resp = await client.delete(
        f"/api/chat/messages/{mid}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["deleted_count"] == 1

    # Message no longer appears in list
    resp = await client.get(
        f"/api/chat/sessions/{sid}/messages",
        cookies={"lore_session": token},
    )
    assert len(resp.json()) == 0


async def test_delete_message_cascade(client, admin_user, project_with_doc):
    """Delete a root message — all descendants are cascade-deleted."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]

    # Build a 3-level tree: root → child → grandchild
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "Root"},
        cookies={"lore_session": token},
    )
    root_id = resp.json()["message_id"]

    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "assistant", "content": "Child", "parent_id": root_id},
        cookies={"lore_session": token},
    )
    child_id = resp.json()["message_id"]

    await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "Grandchild", "parent_id": child_id},
        cookies={"lore_session": token},
    )

    # Delete root — should cascade to all 3
    resp = await client.delete(
        f"/api/chat/messages/{root_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["deleted_count"] == 3

    resp = await client.get(
        f"/api/chat/sessions/{sid}/messages",
        cookies={"lore_session": token},
    )
    assert len(resp.json()) == 0


async def test_delete_message_branch_preserves_siblings(client, admin_user, project_with_doc):
    """Deleting one fork branch preserves the other sibling branch."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]

    # Root → branch_a (+ its child), branch_b (+ its child)
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "Root"},
        cookies={"lore_session": token},
    )
    root_id = resp.json()["message_id"]

    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "assistant", "content": "Branch A", "parent_id": root_id},
        cookies={"lore_session": token},
    )
    branch_a_id = resp.json()["message_id"]
    await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "Branch A child", "parent_id": branch_a_id},
        cookies={"lore_session": token},
    )

    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "assistant", "content": "Branch B", "parent_id": root_id},
        cookies={"lore_session": token},
    )
    branch_b_id = resp.json()["message_id"]
    await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "Branch B child", "parent_id": branch_b_id},
        cookies={"lore_session": token},
    )

    # Delete branch A — should remove branch_a + its child (2)
    resp = await client.delete(
        f"/api/chat/messages/{branch_a_id}",
        cookies={"lore_session": token},
    )
    assert resp.json()["deleted_count"] == 2

    # Root + branch_b + branch_b_child remain
    resp = await client.get(
        f"/api/chat/sessions/{sid}/messages",
        cookies={"lore_session": token},
    )
    remaining = resp.json()
    assert len(remaining) == 3
    remaining_ids = {m["message_id"] for m in remaining}
    assert root_id in remaining_ids
    assert branch_b_id in remaining_ids


async def test_delete_already_deleted_message(client, admin_user, project_with_doc):
    """Deleting an already-deleted message returns 404."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "Gone"},
        cookies={"lore_session": token},
    )
    mid = resp.json()["message_id"]

    await client.delete(f"/api/chat/messages/{mid}", cookies={"lore_session": token})
    # Second delete — 404
    resp = await client.delete(f"/api/chat/messages/{mid}", cookies={"lore_session": token})
    assert resp.status_code == 404


async def test_delete_nonexistent_message(client, admin_user, project_with_doc):
    """Deleting a non-existent message returns 404."""
    _, token = admin_user
    resp = await client.delete(
        "/api/chat/messages/nonexistent123",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


async def test_edit_deleted_message_returns_404(client, admin_user, project_with_doc):
    """Editing a soft-deleted message returns 404."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "Will be deleted"},
        cookies={"lore_session": token},
    )
    mid = resp.json()["message_id"]

    await client.delete(f"/api/chat/messages/{mid}", cookies={"lore_session": token})
    resp = await client.patch(
        f"/api/chat/messages/{mid}",
        json={"content": "Trying to edit deleted"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


async def test_message_with_images(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": "user", "content": "Look at this", "images": ["data:image/png;base64,abc"]},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["images"] == ["data:image/png;base64,abc"]


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


@pytest.mark.asyncio
async def test_models_capability_in_early_return_body(client, admin_user, monkeypatch):
    """Audit fix #2/#3 (A3): /chat/models carries agent availability + reason so the
    frontend can proactively disable the Agent option without a send-time error.
    This covers the empty-AI_API_URL early-return body (the success body is covered
    by test_models_capability_in_success_body)."""
    import driver.client
    # Probe the early-return body by forcing the capability result directly
    # (models_catalog resolves driver.client.agent_capability at call time).
    async def _cap():
        return {"available": False, "reason": "Agent is disabled on this server"}
    monkeypatch.setattr(driver.client, "agent_capability", _cap)
    # WHY: AI_API_URL is non-empty in the test env (dev .env sets it), so
    # without this the handler takes the network branch and makes a
    # real httpx GET to the AI gateway — which times out under full-suite load and
    # 503s (an order-dependent flake). Force the empty-URL early-return path this test
    # actually targets so it never touches the network.
    pin_chat_api(monkeypatch, url="")

    _, token = admin_user
    resp = await client.get("/api/chat/models", cookies={"lore_session": token})
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_available"] is False
    assert data["agent_unavailable_reason"] == "Agent is disabled on this server"


async def test_models_capability_in_success_body(client, admin_user, monkeypatch, http_pool):
    """A3: the same agent availability flags must ride the SUCCESS body (AI_API_URL
    set) alongside the real model list. httpx is mocked so the test never touches the
    network — it verifies the merge, not the gateway."""
    import driver.client
    import routes.chat.models_catalog as comp

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "m2"}, {"id": "m1"}]}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            return _FakeResp()

    async def _cap():
        return {"available": True}

    monkeypatch.setattr(driver.client, "agent_capability", _cap)
    pin_chat_api(monkeypatch)
    # list_models routes through a TTL-cached gateway fetch; reset it so this
    # test forces a fresh fetch via the mocked client (test isolation against
    # other models tests).
    monkeypatch.setattr(comp, "_gateway_models_cache", None)
    http_pool("models_catalog", _FakeClient())

    _, token = admin_user
    resp = await client.get("/api/chat/models", cookies={"lore_session": token})
    assert resp.status_code == 200
    data = resp.json()
    assert data["models"] == ["m1", "m2"]  # sorted
    assert data["agent_available"] is True
    assert data["agent_unavailable_reason"] is None


# ─── Completions ──────────────────────────────────────────────────────────────


async def test_completions_requires_session_owner(client, admin_user, regular_user, project_with_doc):
    """Non-owner cannot call completions."""
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    _, user_token = regular_user

    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": admin_token},
    )
    sid = resp.json()["session_id"]

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 404


# ─── System prompt injection ────────────────────────────────────────────────

# Helper: drive a completion through the driver-owned turn, capturing the wire.


@asynccontextmanager
async def _completion_ctx(captured: list, api_url: str = "http://fake"):
    """Fake the app's driver surfaces (the test_harness_turn harness seams) so
    a POSTed completion runs the real route + prep and hands off to a fake
    driver: the /followup payload carries the composed system_prompt + last
    user turn (what the retired capture fake of the SSE arm saw). On exit the
    payload(s) land in `captured` and each driven turn is closed through the
    channel (lock + heartbeat released), as a live driver would. `api_url`
    pins the AI API links (empty = the unconfigured-gateway shape)."""
    import driver.channel
    import driver.timeline as tl
    import routes.chat.completions as comp
    from driver.client import DriverLine
    from test_driver_channel import FakeConnector, FakeReplay, _env, _turn_end
    from test_harness_turn import FollowupFake, _reset_fanout, _settle

    line = DriverLine(name="pi", url="http://pi.test", secret="s")
    connector = FakeConnector()
    followups = FollowupFake()

    async def _ok_rate(*_a, **_k):
        return True

    async def _no_leaf_move(*_a, **_k):
        # Seam A (the driver-side leaf move) would hit the live harness with
        # the fake line's secret; the branching tests own it.
        return None

    with ExitStack() as stack:
        async def _fake_line():
            return line

        stack.enter_context(patch.object(
            driver.channel, "resolve_driver_line", _fake_line))
        stack.enter_context(patch.object(driver.channel, "_ws_connect", connector))
        stack.enter_context(patch.object(
            driver.channel, "fetch_session_entries", FakeReplay()))
        stack.enter_context(patch.object(driver.channel, "_RECONNECT_MIN_S", 0.01))
        stack.enter_context(patch.object(driver.channel, "_WATCHDOG_TICK_S", 0.01))
        stack.enter_context(patch.object(
            driver.channel, "_SUBSCRIBE_ACK_TIMEOUT_S", 0.2))
        stack.enter_context(patch.object(tl, "post_followup", followups))
        stack.enter_context(patch.object(comp, "resolve_driver_line", _fake_line))
        stack.enter_context(pinned_chat_api(url=api_url))
        stack.enter_context(patch.object(comp, "check_completion_rate_limit", _ok_rate))
        stack.enter_context(patch.object(comp, "_sync_leaf_to_branch_point", _no_leaf_move))
        driver.channel._channel = None
        try:
            yield
        finally:
            captured.extend(followups.payloads)
            # Close each driven turn through the channel (the lock + heartbeat
            # teardown ride on_end), then stop the channel + fan-out pumps.
            for payload in followups.payloads:
                if connector.sockets:
                    sock = connector.sockets[0]
                    sock.push(_env(payload["session_id"],
                                   {"type": "model_update", "model": "test"}))
                    sock.push(_env(payload["session_id"], _turn_end(10)))
            await _settle()
            await driver.channel.get_driver_channel().aclose()
            driver.channel._channel = None
            _reset_fanout()


async def _create_session_and_prompt_doc(client, token, pid, doc_id, prompt_content="You are a pirate"):
    """Helper: create a chat session selecting a persona doc under the system subtree.

    Plan "unify-agent-config": the persona is a `persona`-role child of the
    Personas folder; the session's system_prompt_id selects it. The completion
    body no longer carries system_prompt_id (the session is the single source of
    truth for selection)."""
    from agent_config import ensure_agent_system_docs

    from db import create_record

    roles = await ensure_agent_system_docs(pid)
    persona_id = f"persona-test-{pid}"
    await create_record("documents", persona_id, {
        "project_id": pid, "parent_id": roles["system_prompt"], "title": "Pirate Persona",
        "content": prompt_content, "path": f".lore/system/system_prompt/{persona_id}",
        "is_index": False, "is_system": True, "system_role": "persona", "is_reference": False,
    })
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test-model",
              "system_prompt_id": persona_id},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]
    return sid, persona_id


async def test_completion_without_ai_api_url_reaches_the_driver_gate(
    client, admin_user, project_with_doc,
):
    """No gateway gate on the turn route (the retired CHAT_API_URL check): the
    turn is executed by the driver and the driver's own line gate
    (resolve_driver_line → None) answers availability — so an unset AI_API_URL
    must NOT refuse the POST with 503 'AI API not configured'. Here the line IS
    configured (the fake) and the API pin is empty: the turn is accepted and
    the followup reaches the driver."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test-model"},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]

    captured: list = []

    async with _completion_ctx(captured, api_url=""):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={"messages": [{"role": "user", "content": "Hi"}]},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        assert resp.json()["accepted"] is True

    assert len(captured) == 1, "the turn reached the driver's /followup"


async def test_completions_system_prompt_injected(client, admin_user, project_with_doc):
    """System prompt document content is injected into the Pi system_prompt."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, prompt_doc_id = await _create_session_and_prompt_doc(client, token, pid, doc_id)

    captured: list = []

    async with _completion_ctx(captured):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={
                "messages": [{"role": "user", "content": "Ahoy!"}],
            },
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        _ = resp.text

    assert len(captured) == 1
    system_prompt = captured[0]["system_prompt"]
    # The selected persona is injected into the composed system_prompt (once).
    assert "You are a pirate" in system_prompt
    # The last user turn travels as `prompt`, not replayed in system_prompt history.
    _assert_prompt_text(captured[0]["prompt"], "Ahoy!")


async def test_completions_system_prompt_plus_document_context(client, admin_user, project_with_doc):
    """When both system_prompt_id and document_id are set, system prompt comes first."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, prompt_doc_id = await _create_session_and_prompt_doc(client, token, pid, doc_id)

    # Create a regular document to "talk to"
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "My Lore", "content": "The kingdom fell in 1042."},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]

    captured: list = []

    async with _completion_ctx(captured):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={
                "messages": [{"role": "user", "content": "Tell me about the kingdom"}],
                "context_ids": [doc_id],
            },
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        _ = resp.text

    assert len(captured) == 1
    system_prompt = captured[0]["system_prompt"]
    # Both the document scope line and the selected persona are folded into the
    # composed system_prompt; the user turn travels as `prompt`.
    assert "You are a pirate" in system_prompt
    assert "My Lore" in system_prompt
    assert "The kingdom fell in 1042." not in system_prompt
    # The persona lives INSIDE the cacheable base (after bootstrap); the document
    # scope (from build_context) is APPENDED after it, because context is the
    # turn-varying block and must not sit inside the prompt-cache prefix. So the
    # persona precedes the document scope line.
    assert system_prompt.index("You are a pirate") < system_prompt.index("My Lore")
    _assert_prompt_text(captured[0]["prompt"], "Tell me about the kingdom")


async def test_completions_only_document_context(client, admin_user, project_with_doc):
    """Regression: document context injection still works without system_prompt_id."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user

    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]

    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Lore Doc", "content": "Dragons exist."},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]

    captured: list = []

    async with _completion_ctx(captured):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={
                "messages": [{"role": "user", "content": "Tell me about dragons"}],
                "context_ids": [doc_id],
            },
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        _ = resp.text

    assert len(captured) == 1
    system_prompt = captured[0]["system_prompt"]
    # Document scope (title + id) injected into the composed system_prompt; the
    # body itself does not ride — the agent reads it on demand.
    assert "Lore Doc" in system_prompt
    assert "Dragons exist." not in system_prompt
    _assert_prompt_text(captured[0]["prompt"], "Tell me about dragons")


async def test_completions_no_system_messages(client, admin_user, project_with_doc):
    """Without system_prompt_id or document context, the composed system_prompt
    carries only the agent persona (no user-driven system content)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user

    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]

    captured: list = []

    async with _completion_ctx(captured):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={"messages": [{"role": "user", "content": "Hello"}]},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        _ = resp.text

    assert len(captured) == 1
    # No user-driven system content injected; the last user turn is the prompt.
    _assert_prompt_text(captured[0]["prompt"], "Hello")


async def test_completions_system_prompt_wrong_project(client, admin_user, project_with_doc):
    """System prompt doc from a different project is NOT injected."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user

    resp = await client.post("/api/projects", json={"name": "Other"}, cookies={"lore_session": token})
    other_pid = resp.json()["project_id"]
    resp = await client.post(
        "/api/documents",
        json={"project_id": other_pid, "title": "Alien Prompt", "content": "You are an alien"},
        cookies={"lore_session": token},
    )
    alien_doc_id = resp.json()["document_id"]

    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]

    captured: list = []

    async with _completion_ctx(captured):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={
                "messages": [{"role": "user", "content": "Hi"}],
                "system_prompt_id": alien_doc_id,
            },
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        _ = resp.text

    assert len(captured) == 1
    system_prompt = captured[0]["system_prompt"]
    # Alien prompt rejected (wrong project) — its content must NOT be injected.
    assert "You are an alien" not in system_prompt


async def test_completions_system_prompt_deleted_doc(client, admin_user, project_with_doc):
    """Deleted system prompt document is NOT injected."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, prompt_doc_id = await _create_session_and_prompt_doc(client, token, pid, doc_id)

    # Soft-delete the prompt doc
    await client.delete(f"/api/documents/{prompt_doc_id}", cookies={"lore_session": token})

    captured: list = []

    async with _completion_ctx(captured):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={
                "messages": [{"role": "user", "content": "Hi"}],
            },
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        _ = resp.text

    assert len(captured) == 1
    system_prompt = captured[0]["system_prompt"]
    # Deleted persona must NOT be injected (session still references it, but the
    # doc is gone → build_agent_system_prompt finds no match).
    assert "You are a pirate" not in system_prompt


async def test_completions_system_prompt_empty_content(client, admin_user, project_with_doc):
    """System prompt document with empty content is NOT injected."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, prompt_doc_id = await _create_session_and_prompt_doc(client, token, pid, doc_id, prompt_content="")

    captured: list = []

    async with _completion_ctx(captured):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={
                "messages": [{"role": "user", "content": "Hi"}],
            },
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        _ = resp.text

    assert len(captured) == 1
    # Empty-content persona injects nothing of its own; the turn still completes.
    _assert_prompt_text(captured[0]["prompt"], "Hi")


async def test_completions_system_prompt_nonexistent(client, admin_user, project_with_doc):
    """A session selecting a nonexistent persona id does not crash — nothing injected."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user

    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test",
              "system_prompt_id": "nonexistent-doc-999"},
        cookies={"lore_session": token},
    )
    sid = resp.json()["session_id"]

    captured: list = []

    async with _completion_ctx(captured):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={
                "messages": [{"role": "user", "content": "Hi"}],
            },
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        _ = resp.text

    assert len(captured) == 1
    # Nonexistent persona is skipped (no crash); the turn still completes.
    _assert_prompt_text(captured[0]["prompt"], "Hi")


async def test_completions_system_prompt_respects_session_ownership(client, admin_user, regular_user, project_with_doc):
    """Non-owner cannot call completions on another user's session."""
    pid, doc_id, _ = project_with_doc
    _, admin_token = admin_user
    _, user_token = regular_user

    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": admin_token},
    )
    sid = resp.json()["session_id"]

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
        },
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 404
