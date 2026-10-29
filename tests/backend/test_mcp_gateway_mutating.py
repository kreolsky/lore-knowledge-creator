"""Integration tests for the MCP gateway — mutating tools (binary key model).

Plan "consistent work-area": the MCP surface has NO proposal/confirmation step.
A read-write key (api_keys.auto_apply=True) applies every mutating write directly
and returns {status:"applied"} (reversible via a pre-edit checkpoint in History);
a read-only key (auto_apply falsy) is rejected with 403 at dispatch. There is no
`proposed` state and no get_proposal_status tool.
"""

import hashlib
import json
import secrets

import pytest

# ─── Test helpers (shared shape with the readonly suite) ──────────────────────


async def _make_agent_key(test_db, user_id: str, project_id: str, *, auto_apply: bool = False) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"mcp-mut-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id,
        "document_id": "", "token_hash": token_hash, "label": "agent",
        "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


def _mcp_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


async def _make_doc(client, token: str, project_id: str, title: str, content: str = "") -> str:
    resp = await client.post(
        "/api/documents",
        json={"project_id": project_id, "title": title, "content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


def _rpc(method: str, params: dict | None = None, *, id_: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def _result_text(resp_json: dict) -> object:
    result = resp_json["result"]
    return json.loads(result["content"][0]["text"])


def _is_error(resp_json: dict) -> bool:
    return resp_json.get("result", {}).get("isError", False)


async def _mcp_call(client, token: str, name: str, arguments: dict | None = None) -> dict:
    resp = await client.post(
        "/mcp", json=_rpc("tools/call", {"name": name, "arguments": arguments or {}}),
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ─── read-write key: writes apply directly ────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_document_full_rewrite_is_422_split_next_action(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A whole-document old_string is rejected as full_rewrite with 422 (NOT the
    409 stale loop): next_action steers to splitting into small edits, and the
    message steers to a sequence of pointwise edits — never create_document."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    body = "The whole document body that will be resent verbatim."
    doc_id = await _make_doc(client, admin_token, pid, "RewriteMe", body)
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": doc_id, "old_string": body, "new_string": "x",
    })
    assert _is_error(data)
    payload = _result_text(data)
    assert payload["status_code"] == 422
    assert payload["next_action"] == "split into smaller edits"
    # The detail is now a dict {index, error} (batch carries the failing index).
    assert "SEQUENCE of small edit_document calls" in payload["error"]["error"]


# ─── system docs are not editable over MCP ────────────────────────────────────


@pytest.mark.asyncio
async def test_system_doc_edit_rejected_over_mcp(client, mcp_running, test_db, admin_user, project_with_doc):
    """Even a read-write key cannot edit an is_system doc over MCP — the agent's
    own safety config is not force-applicable; it is rejected, not proposed."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    from agent_config import ensure_agent_system_docs
    skeleton = await ensure_agent_system_docs(pid)
    rules_id = skeleton["rules_folder"]

    read = await _mcp_call(client, agent_tok, "read_document", {"document_id": rules_id})
    content = _result_text(read)["content"]
    # Pick a verbatim substring to satisfy the edit-range resolver.
    needle = content.strip().split("\n", 1)[0][:12] or "Rules"

    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": rules_id, "old_string": needle, "new_string": needle + "X",
    })
    assert _is_error(data)
    assert _result_text(data)["status_code"] == 403
    assert "System documents" in _result_text(data)["error"]


# ─── uniform 404 for cross-project / missing targets (read-write key) ─────────


@pytest.mark.asyncio
async def test_create_document_cross_project_uniform_404(client, mcp_running, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)
    data = await _mcp_call(client, agent_tok, "create_document", {
        "title": "Child", "content": "x", "parent_id": "doc-not-in-project",
    })
    assert _is_error(data)
    assert _result_text(data)["status_code"] == 404


@pytest.mark.asyncio
async def test_edit_document_cross_project_uniform_404(client, mcp_running, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)
    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": "doc-not-in-project", "old_string": "a", "new_string": "b",
    })
    assert _is_error(data)
    assert _result_text(data)["status_code"] == 404


# ─── CSRF Origin exemption for /mcp ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_mutating_call_with_foreign_origin_is_allowed(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """The MCP gateway authenticates via the Bearer header only and never reads the
    lore_session cookie, so a foreign Origin must NOT be rejected by the CSRF guard."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Origin Test", "Alpha beta.")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    resp = await client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "edit_document", "arguments": {
            "document_id": doc_id, "old_string": "Alpha", "new_string": "Omega",
        }}),
        headers={**_mcp_headers(agent_tok), "Origin": "http://evil.example"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert not _is_error(data), f"foreign Origin was rejected (CSRF false positive): {data}"
    assert _result_text(data)["status"] == "applied"


# ─── Transport rate limit (F3) — pre-auth flood ceiling at the ASGI gate ──────


@pytest.mark.asyncio
async def test_transport_rate_limit_trips_on_spam(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """F3: the ASGI gate enforces a transport-level flood ceiling BEFORE the
    stateless session manager runs. tools/list is served with no per-call DB auth
    (the session manager owns its routing), so without this gate an
    unauthenticated flood — or list spam — reaches the manager unbounded. A 4th
    request past the (monkeypatched) cap returns a RAW HTTP 429 — distinct from
    the per-call MCP isError 429 of the mutating tier, because it never reaches
    handle_request (no JSON-RPC body, just {detail}). Keyed on the full token hash."""
    import rate_limit as _rl

    # A small cap keeps the test fast + deterministic; the real ceiling is 240/60s.
    # No store-clear needed: the tier is Redis-backed and conftest's per-test
    # FLUSHDB resets the budget between tests.
    monkeypatch.setattr(_rl._tiers["mcp_transport"], "max", 3)

    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)
    headers = _mcp_headers(token)

    # 3 requests pass the transport gate and reach the session manager (200).
    for i in range(3):
        resp = await client.post("/mcp", json=_rpc("tools/list"), headers=headers)
        assert resp.status_code == 200, f"call {i} unexpectedly rejected: {resp.status_code}"

    # The 4th trips the tier → raw HTTP 429 BEFORE handle_request.
    resp = await client.post("/mcp", json=_rpc("tools/list"), headers=headers)
    assert resp.status_code == 429
    assert resp.json() == {"detail": "Too many requests"}


@pytest.mark.asyncio
async def test_transport_ip_limit_bounds_token_rotation(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """The per-token transport bucket cannot bound a flood that ROTATES tokens
    (each bogus Bearer gets a fresh bucket) — the per-IP tier bounds exactly
    that rotation. With the IP cap monkeypatched to 3, the 4th request from one
    address is a raw HTTP 429 while NO per-token bucket tripped (each token was
    seen once). Keyed on X-Real-IP, the header the edge overwrites."""
    import rate_limit as _rl

    monkeypatch.setattr(_rl._tiers["mcp_transport_ip"], "max", 3)

    headers = {"X-Real-IP": "198.51.100.77"}
    for i in range(3):
        resp = await client.post(
            "/mcp", json=_rpc("tools/list"),
            headers={**_mcp_headers(f"lore_bogus_{i}"), **headers},
        )
        assert resp.status_code != 429, f"request {i} tripped a bucket unexpectedly"

    resp = await client.post(
        "/mcp", json=_rpc("tools/list"),
        headers={**_mcp_headers("lore_bogus_3"), **headers},
    )
    assert resp.status_code == 429
    assert resp.json() == {"detail": "Too many requests"}
