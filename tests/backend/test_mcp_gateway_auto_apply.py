"""Binary key model — read-write vs read-only over MCP (plan "consistent work-area").

api_keys.auto_apply is reinterpreted: True = read-write (every MCP mutating write
applies directly + checkpoint, reversible in the Lore History panel); anything
falsy = read-only (mutating tools 403 at dispatch). The agent's `apply` arg is
IGNORED over MCP (there is no proposed state). RBAC is still an independent wall
(a read-write key on a read-only membership is 403), and the agent's own system
docs are not editable over MCP (403, not proposed). The Tool-API HTTP path and Pi
path keep the proposal machinery — only the MCP branch is always-auto.

Audit: the acting key_id is recorded on the agent-tool telemetry row.
"""

import asyncio
import hashlib
import json
import secrets

import pytest

# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _make_agent_key(
    test_db, user_id: str, project_id: str, *, auto_apply: bool = False,
    key_id: str | None = None,
) -> tuple[str, str]:
    """Mint an agent key (returns token + the key_id, for audit assertions)."""
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = key_id or f"mcp-aa-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id,
        "document_id": "", "token_hash": token_hash, "label": "agent",
        "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token, key_id


def _mcp_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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


def _payload(resp_json: dict) -> object:
    return json.loads(resp_json["result"]["content"][0]["text"])


def _is_error(resp_json: dict) -> bool:
    return resp_json.get("result", {}).get("isError", False)


async def _mcp_call(client, token: str, name: str, arguments: dict | None = None) -> dict:
    resp = await client.post(
        "/mcp", json=_rpc("tools/call", {"name": name, "arguments": arguments or {}}),
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _add_member(test_db, project_id: str, user_id: str, level: str) -> None:
    pm_id = f"mcp-aa-pm-{project_id}-{user_id}"
    await test_db.query("DELETE type::record('project_members', $id)", {"id": pm_id})
    await test_db.query(
        "CREATE type::record('project_members', $id) SET project_id = $pid, "
        "user_id = $uid, access_level = $lvl",
        {"id": pm_id, "pid": project_id, "uid": user_id, "lvl": level},
    )


async def _read_content(client, token: str, doc_id: str) -> str:
    read = await _mcp_call(client, token, "read_document", {"document_id": doc_id})
    return _payload(read)["content"]


# ─── #1: granted key + edit (no apply arg) → applied + checkpoint + mutated ───


@pytest.mark.asyncio
async def test_granted_key_auto_applies_without_apply_arg(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Ledger", "Balance is 100 gold.")
    agent_tok, key_id = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": doc_id, "old_string": "100 gold", "new_string": "200 gold",
        # NOTE: no "apply" arg — the grant closes the loop by default.
    })
    assert not _is_error(data)
    payload = _payload(data)
    assert payload["status"] == "applied", "granted key must auto-apply (no apply arg)"

    assert "200 gold" in await _read_content(client, agent_tok, doc_id)

    from db import get_db
    db = await get_db()
    cps = await db.query(
        "SELECT id FROM checkpoints WHERE document_id = $d", {"d": doc_id},
    )
    assert cps, "expected a pre-edit checkpoint after a granted auto-apply"
    return key_id  # consumed by the audit test below


# ─── #2: read-write key BUT system doc → 403 (rejected, not proposed) ─────────


@pytest.mark.asyncio
async def test_readwrite_key_system_doc_rejected(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "System Tome", "secret config")
    await test_db.query(
        "UPDATE type::record('documents', $id) SET is_system = true", {"id": doc_id},
    )
    agent_tok, _ = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": doc_id, "old_string": "secret", "new_string": "public",
    })
    assert _is_error(data), "a system doc is not editable over MCP"
    payload = _payload(data)
    assert payload["status_code"] == 403
    assert "System documents" in payload["error"]
    assert "secret config" in await _read_content(client, agent_tok, doc_id)


# ─── #3: read-only key + apply:auto → 403 (apply arg is ignored over MCP) ─────


@pytest.mark.asyncio
async def test_readonly_key_apply_auto_still_rejected(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Codex", "Original verse.")
    agent_tok, _ = await _make_agent_key(test_db, admin_uid, pid, auto_apply=False)

    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": doc_id, "old_string": "Original", "new_string": "Rewritten",
        "apply": "auto",  # ignored — a read-only key can never write over MCP
    })
    assert _is_error(data), "a read-only key sending apply=auto must still be rejected"
    assert _payload(data)["status_code"] == 403
    assert "Original verse." in await _read_content(client, agent_tok, doc_id), (
        "doc must be UNCHANGED"
    )


# ─── #4: read-write key + apply:confirm → still applied (apply arg ignored) ───


@pytest.mark.asyncio
async def test_readwrite_key_apply_confirm_ignored_still_applies(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Manual", "Step one.")
    agent_tok, _ = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": doc_id, "old_string": "Step one", "new_string": "Step zero",
        "apply": "confirm",  # no confirmation step over MCP — the arg is ignored
    })
    payload = _payload(data)
    assert payload["status"] == "applied", (
        "over MCP the apply arg is ignored — a read-write key always applies"
    )
    assert "Step zero." in await _read_content(client, agent_tok, doc_id)


# ─── #5: granted key on read-only membership → 403 (grant never widens access)


@pytest.mark.asyncio
async def test_granted_key_readonly_membership_still_rejected(
    client, mcp_running, test_db, admin_user, regular_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    reg_uid, _ = regular_user
    # A real doc (an absent id would 404 before the RBAC check is reached).
    doc_id = await _make_doc(client, admin_token, pid, "Guarded", "sealed content")
    await _add_member(test_db, pid, reg_uid, "readonly")
    agent_tok, _ = await _make_agent_key(test_db, reg_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": doc_id, "old_string": "sealed", "new_string": "opened",
    })
    assert _is_error(data), "a read-only member must be rejected even with a grant"
    assert _payload(data)["status_code"] == 403


# ─── #6: init reports capabilities.writable matching the key ──────────────────


@pytest.mark.asyncio
async def test_init_capabilities_writable_matches_key(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    pid, _, admin_uid = project_with_doc

    rw_tok, _ = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)
    pkg = _payload(await _mcp_call(client, rw_tok, "init", {}))
    assert pkg["capabilities"]["writable"] is True, "a read-write key's init must advertise writable"

    ro_tok, _ = await _make_agent_key(test_db, admin_uid, pid, auto_apply=False)
    pkg2 = _payload(await _mcp_call(client, ro_tok, "init", {}))
    assert pkg2["capabilities"]["writable"] is False, "a read-only key's init must advertise not-writable"


# ─── #7: auto-apply records the acting key_id (audit source-of-truth) ─────────


@pytest.mark.asyncio
async def test_auto_apply_records_key_id_on_telemetry(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """The acting key_id is recorded on the agent-tool telemetry row — the system
    fact at apply time, queryable for audit of granted auto-applies."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Audit Doc", "v1 content")
    agent_tok, key_id = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": doc_id, "old_string": "v1", "new_string": "v2",
    })
    assert _payload(data)["status"] == "applied"

    from db import get_db
    db = await get_db()
    found = None
    for _ in range(20):  # telemetry insert is fire-and-forget — poll briefly
        rows = await db.query(
            "SELECT detail FROM telemetry_event "
            "WHERE category = 'agent_tool' AND project_id = $pid "
            "AND detail.key_id = $kid",
            {"pid": pid, "kid": key_id},
            site="q",
        )
        if rows:
            found = rows[0]["detail"]
            break
        await asyncio.sleep(0.05)
    assert found is not None, "no telemetry row carrying the acting key_id"
    assert found.get("tool") == "edit_document"
    assert found.get("result_status") == "applied"
    assert found.get("key_id") == key_id


# ─── #8: Tool-API HTTP path unchanged (the containment proof) ─────────────────


@pytest.mark.asyncio
async def test_tool_api_http_path_unchanged_ungranted_auto(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """An existing (un-granted) agent key hitting POST /api/tool/edit_document with
    apply=auto behaves EXACTLY as before Slice 3 — the grant gate is MCP-only, the
    HTTP handler still reads body.apply.value directly (ignores ctx.auto_apply)."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "HTTP Doc", "alpha value")
    agent_tok, _ = await _make_agent_key(test_db, admin_uid, pid, auto_apply=False)

    resp = await client.post(
        "/api/tool/edit_document",
        json={
            "document_id": doc_id, "old_string": "alpha", "new_string": "beta",
            "apply": "auto",
        },
        headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied", (
        "HTTP path apply=auto must still apply (behavior unchanged by the MCP grant)"
    )
    assert "beta value" in await _read_content(client, agent_tok, doc_id)
