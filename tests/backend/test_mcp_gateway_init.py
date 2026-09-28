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


@pytest.mark.asyncio
async def test_init_rules_include_seeded_defaults(client, mcp_running, test_db, admin_user, project_with_doc):
    """The rules subtree is rendered inline and includes the seeded _DEFAULT_RULES
    content (lazy-init seeds rules_folder with defaults on first agent use)."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    from agent_config import _DEFAULT_RULES

    pkg = _payload(await _init(client, token))
    # Derived from the seed constant rather than quoting one of its phrases: the
    # seeded body is model-facing prose that gets reworded, and a quoted phrase
    # makes every rewording fail here while proving nothing about what was served.
    seeded_line = next(
        ln.strip() for ln in _DEFAULT_RULES.splitlines() if len(ln.strip()) > 30
    )
    assert seeded_line in pkg["rules"]
    assert pkg["rules"].strip().startswith("# Rules")


@pytest.mark.asyncio
async def test_init_indexes_carry_no_content(client, mcp_running, test_db, admin_user, project_with_doc):
    """skills_index / knowledge_index are id+title read-on-demand pointers — they
    must NOT carry content (the harness reads it via read_document when needed)."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    pkg = _payload(await _init(client, token))
    for index_key in ("skills_index", "knowledge_index"):
        index = pkg[index_key]
        assert isinstance(index, list)
        for entry in index:
            assert set(entry.keys()) <= {"id", "title"}, (
                f"{index_key} entry has content keys: {entry.keys()}"
            )


@pytest.mark.asyncio
async def test_init_persona_is_null_in_v1(client, mcp_running, test_db, admin_user, project_with_doc):
    """Persona is a session concept; the gateway omits it in v1 (null)."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    pkg = _payload(await _init(client, token))
    assert pkg["persona"] is None


@pytest.mark.asyncio
async def test_init_idempotent_no_duplicate_system_docs(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Calling init twice does not create duplicate system docs (ensure_agent_system_docs
    is idempotent via deterministic ids)."""
    from db import get_db

    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    await _init(client, token)
    db = await get_db()
    after_first = await db.query(
        "SELECT count() AS n FROM documents "
        "WHERE project_id = $pid AND is_system = true AND deleted_at IS NONE GROUP ALL",
        {"pid": pid},
    )
    n_first = (after_first[0]["n"] if after_first else 0)

    await _init(client, token)
    after_second = await db.query(
        "SELECT count() AS n FROM documents "
        "WHERE project_id = $pid AND is_system = true AND deleted_at IS NONE GROUP ALL",
        {"pid": pid},
    )
    n_second = (after_second[0]["n"] if after_second else 0)

    assert n_first == n_second, f"init created duplicate system docs ({n_first} → {n_second})"
    assert n_first > 0


@pytest.mark.asyncio
async def test_init_instructions_carry_immutable_contract(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """The GATEWAY_BOOTSTRAP immutable tier states the load-bearing contract:
    writes apply directly (no approval step), read-on-demand, editing + error
    contract."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    instructions = _payload(await _init(client, token))["instructions"]
    for phrase in ("applied", "read-write", "read_document",
                   "old_string", "full_rewrite", "409", "429", "next_action"):
        assert phrase in instructions, f"GATEWAY_BOOTSTRAP missing {phrase!r}"
    # The removed proposal/approval vocabulary must be gone from the MCP contract.
    assert "proposed" not in instructions
    assert "get_proposal_status" not in instructions


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


@pytest.mark.asyncio
async def test_init_unscoped_key_keeps_full_contract(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    pkg = _payload(await _init(client, token))
    assert pkg["capabilities"]["scope"] is None
    assert "\nSCOPE\n" not in pkg["instructions"]


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


async def test_instructions_prose_never_names_an_unroutable_tool():
    """Name-level prose guard: every tool name the init package teaches in its
    hand-written prose must be dispatchable. The create-vs-upload boundary used
    to send harnesses to `import_file` — a Pi-only tool this gateway 404s — while
    the MCP byte channel is `attach_file`. The tool-name LISTS are already bound
    to tools/list by test_init_instructions_list_every_served_tool_name; this
    guards the prose, which is judgment and not derivable."""
    import re

    from agent_tools.registry import mcp_entries
    from mcp_gateway.bootstrap import _build_instructions

    text = await _build_instructions(scope_root=None, root_title=None)
    snake = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", text))
    # Prose vocabulary that names parameters/error kinds, not tools.
    params = {
        "old_string", "new_string", "old_value", "next_action", "full_rewrite",
        "status_code", "parent_id", "document_id",
    }
    routed = {e.name for e in mcp_entries()}  # preview_extractor incl.: prose may teach it
    named = snake - params
    unroutable = sorted(n for n in named if n not in routed)
    assert not unroutable, (
        f"init prose names tools that 404 on dispatch: {unroutable}"
    )
    assert "attach_file" in named, (
        "the byte-channel boundary must name attach_file (the MCP upload tool)"
    )
