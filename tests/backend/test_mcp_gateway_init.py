"""Integration tests for the MCP gateway — init bootstrap (Slice 3).

`init` returns a tiered bootstrap package assembled from the project's agent
config subtree (.lore/system) + an immutable code constant (GATEWAY_BOOTSTRAP).
The external harness calls it once on connect to receive rules, a tool protocol,
and read-on-demand indexes — replacing per-harness file scaffolding.
"""

import hashlib
import json
import secrets

import pytest

# ─── helpers ──────────────────────────────────────────────────────────────────


async def _make_agent_key(test_db, user_id: str, project_id: str) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"mcp-init-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id,
        "document_id": "", "token_hash": token_hash, "label": "agent",
        "capabilities": ["agent"],
    })
    return token


def _mcp_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


async def _init(client, token: str) -> dict:
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "init", "arguments": {}}},
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _payload(resp_json: dict) -> dict:
    return json.loads(resp_json["result"]["content"][0]["text"])


# ─── init package shape ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_returns_required_keys(client, mcp_running, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    data = await _init(client, token)
    assert not data["result"].get("isError")
    pkg = _payload(data)

    for key in (
        "protocol_version", "project", "acting_user", "instructions",
        "rules", "persona", "skills_index", "knowledge_index",
    ):
        assert key in pkg, f"init package missing key {key!r}"

    assert pkg["protocol_version"] == 1
    assert pkg["project"]["id"] == pid
    assert pkg["project"]["name"]
    assert pkg["acting_user"]["user_id"] == admin_uid
    assert pkg["acting_user"]["name"]


# ─── scope-aware init (subtree-scoped keys) ───────────────────────────────────


async def _make_scoped_key(test_db, user_id: str, project_id: str, doc_id: str) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"mcp-init-scoped-{secrets.token_hex(4)}", {
        "user_id": user_id, "project_id": project_id,
        "document_id": doc_id, "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "scoped", "capabilities": ["agent"],
    })
    return token


@pytest.mark.asyncio
async def test_init_scoped_key_advertises_scope(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A subtree-scoped key's init package must carry capabilities.scope and
    include a SCOPE section naming the subtree root."""
    pid, doc_id, admin_uid = project_with_doc
    token = await _make_scoped_key(test_db, admin_uid, pid, doc_id)

    pkg = _payload(await _init(client, token))
    assert pkg["capabilities"]["scope"] == {
        "root_id": doc_id,
        "root_title": pkg["capabilities"]["scope"]["root_title"],
    }
    assert pkg["capabilities"]["scope"]["root_title"]
    instructions = pkg["instructions"]
    assert "SCOPE" in instructions
    assert doc_id in instructions


async def test_scope_section_renders_the_single_out_of_scope_detail():
    """The SCOPE section's out-of-scope 403 sentence is rendered from the SAME
    single producer the wall raises (scope.out_of_scope_detail), not a hand-copied
    second string — so the instructions cannot drift from what the agent actually
    sees on a 403. Asserted on the embedded substring (the root is unknown at
    module-definition time, so the section is filled per-call via a sentinel)."""
    from mcp_gateway.bootstrap import _build_instructions

    from scope import out_of_scope_detail

    root = "scope-root-1234"
    instructions = await _build_instructions(scope_root=root, root_title="Sandbox")
    assert out_of_scope_detail(root) in instructions
