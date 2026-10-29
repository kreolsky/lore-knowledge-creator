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
