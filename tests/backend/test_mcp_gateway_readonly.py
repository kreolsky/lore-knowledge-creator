"""Integration tests for the MCP Universal Agent Gateway — read-only slice (plan
"mcp-universal-agent-gateway", Slice 1).

The gateway mounts a streamable-HTTP MCP server at /mcp inside the backend. It
reuses the existing agent Tool-API executors (no new business logic) and
authenticates a project-scoped agent key (Bearer) with a per-call live RBAC
re-check — same semantics as routes.tool_api.get_agent_context.

These are contract tests speaking raw JSON-RPC to /mcp (stateless + json-response
mode ⇒ one POST per request, no SSE, no separate initialize).

# ARCH: parity guard — the gateway must return the SAME data the Tool-API HTTP
# surface returns, because the gateway dispatches to the SAME executors.
"""

import hashlib
import json
import secrets

import pytest

# ─── Test helpers ─────────────────────────────────────────────────────────────


async def _make_agent_key(test_db, user_id: str, project_id: str) -> str:
    """Insert a project-scoped agent API key and return the plaintext token."""
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"mcp-agent-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": "",  # project-scoped
        "token_hash": token_hash,
        "label": "agent",
        "capabilities": ["agent"],
    })
    return token


async def _make_scoped_agent_key(test_db, user_id: str, project_id: str, doc_id: str) -> str:
    """Insert a subtree-scoped agent API key (document_id = scope root) and return
    the plaintext token."""
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"mcp-scoped-{secrets.token_hex(4)}", {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": doc_id,  # subtree scope root
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "scoped",
        "capabilities": ["agent"],
    })
    return token


async def _make_doc(client, token: str, project_id: str, title: str, content: str = "") -> str:
    resp = await client.post(
        "/api/documents",
        json={"project_id": project_id, "title": title, "content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


def _rpc(method: str, params: dict | None = None, *, id_: int = 1) -> dict:
    return {
        "jsonrpc": "2.0", "id": id_, "method": method,
        "params": params or {},
    }


def _hdr(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _mcp_headers(token: str) -> dict:
    """Headers a compliant MCP client sends: Authorization + the Accept header the
    streamable-HTTP transport requires in json-response mode."""
    h = _hdr(token)
    h["Accept"] = "application/json"
    return h


def _result_text(resp_json: dict) -> object:
    """Parse the JSON payload carried by a tools/call content[0].text."""
    result = resp_json["result"]
    content = result["content"]
    assert content, f"expected content blocks, got {result}"
    return json.loads(content[0]["text"])


def _is_error(resp_json: dict) -> bool:
    return resp_json.get("result", {}).get("isError", False)


async def _mcp_call(client, token: str, name: str, arguments: dict | None = None) -> dict:
    resp = await client.post(
        "/mcp", json=_rpc("tools/call", {"name": name, "arguments": arguments or {}}),
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _mcp_list(client, token: str) -> dict:
    resp = await client.post("/mcp", json=_rpc("tools/list"), headers=_mcp_headers(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _every_served_text(tools: list[dict]) -> list[tuple[str, str]]:
    """(which, text) for every description string a model reads off tools/list:
    each tool's description + each advertised property's description."""
    out: list[tuple[str, str]] = []
    for t in tools:
        out.append((t["name"], t.get("description") or ""))
        for pname, p in (t.get("inputSchema", {}).get("properties") or {}).items():
            if isinstance(p, dict) and p.get("description"):
                out.append((f"{t['name']}.{pname}", p["description"]))
    return out


# ─── tools/list renders the caller's scope + authenticates like tools/call ───


@pytest.mark.asyncio
async def test_tools_list_scoped_key_names_its_own_root(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A subtree-scoped key's tools/list names the key's OWN root (title + id) in
    the create/move tool texts AND their parent_id params — the executor resolves
    a null parent there (scope.resolve_scoped_parent), so the served text must say
    so instead of 'project root', a place the key cannot reach."""
    from db import fetch_one

    pid, root_doc, admin_uid = project_with_doc
    root = await fetch_one("documents", root_doc)
    assert root, "fixture doc missing"
    scoped = await _make_scoped_agent_key(test_db, admin_uid, pid, root_doc)

    data = await _mcp_list(client, scoped)
    tools = {t["name"]: t for t in data["result"]["tools"]}
    expected = f'the "{root["title"]}" subtree root ({root_doc})'
    for name in ("create_document", "move_document"):
        assert expected in tools[name]["description"], name
        pdesc = tools[name]["inputSchema"]["properties"]["parent_id"]["description"]
        assert expected in pdesc, f"{name}.parent_id"
    # The WHOLE surface — every tool and param text — is project-root-free and
    # sentinel-free.
    for which, text in _every_served_text(data["result"]["tools"]):
        assert "project root" not in text, which
        assert "{{ROOT}}" not in text, which


# ─── tools/list + schema anti-drift ───────────────────────────────────────────


def test_tools_list_curated_order(monkeypatch):
    """tools/list emits the curated weak-model order: init → reads → text → file →
    tree → tables (schemas._TOOL_LIST_ORDER), grouped by the PART of a node.

    MCP_RUN_EXTRACTOR is pinned OFF: it is an env gate (_preview_extractor_visible
    reads config at call time), so a deployment that sets it — this repo's own dev
    .env does — would otherwise make this exact-list assertion pass or fail on the
    runner's environment rather than on the curated order. The gate itself is bound
    by test_tool_surface_consolidation.test_run_extractor_not_advertised_by_default
    (OFF) and test_mcp_gateway_run_extractor.test_run_extractor_advertised (ON).
    """
    import config
    monkeypatch.setattr(config, "MCP_RUN_EXTRACTOR", False)
    from mcp_gateway.schemas import build_tool_list

    names = [t.name for t in build_tool_list()]
    assert names == [
        "init",
        "search_materials", "read_document", "get_project_structure",
        "export_subtree_md",
        "create_document", "edit_document", "append_to_document",
        "attach_file", "get_file", "reprocess_file",
        "move_document", "rename_document", "import_subtree_md",
        "create_table", "edit_table_cell", "add_table_rows", "add_table_column",
        # D14: import_file is Pi-only now (MCP serves attach_file). 18 tools by
        # default, counting init like the redesign suite's pin.
    ], names


# ─── advertised-vs-enforced classification parity ────────────────────────────


def _instruction_tool_lists(text: str) -> tuple[set[str], set[str]]:
    """Parse the two rendered tool lines out of the init instructions text and
    return (read_names, mutating_names).

    Parsed per-LINE (not a global substring): a tool name can also appear in the
    surrounding prose (e.g. `read_document` in the EDITING CONTRACT), so a whole-
    text substring check would false-pass a name into a list it isn't in. Each
    line is `- <Marker> — a, b, c.`; the names are the comma-split slice between
    the em-dash and the first period."""
    def _after(marker: str) -> set[str]:
        line = next(l for l in text.splitlines() if l.lstrip().startswith(marker))
        rest = line.split("—", 1)[1]
        return {n.strip() for n in rest.split(".")[0].split(",") if n.strip()}

    return _after("- Read tools"), _after("- Mutating tools")


async def test_instructions_classification_matches_enforcement_parity(monkeypatch):
    """Classification parity: for EVERY tool build_tool_list() advertises, the list
    it lands in inside the rendered init instructions (the `- Read tools` /
    `- Mutating tools` LINES) must equal the classification `server.call_tool`
    enforces — the registry entry's `mutating` flag ⇒ mutating, otherwise read.

    Two mechanics the assertion needs:
      - `init` is advertised but prose-only (its own bullet, never in either list).
        Asserting it sits in NEITHER list keeps a future misclassification of it
        visible rather than silently skipped.
      - membership is parsed from the two rendered LINES, not a global substring.

    # ARCH: this is the test the upload_reference_file bug existed under — the
    # instructions advertised it as a read tool while server.call_tool gated it as
    # a write. Both sides now read the same registry flag; a second hand-kept
    # mutating set re-appearing is exactly what fails this test loudly.
    """
    import config

    monkeypatch.setattr(config, "MCP_RUN_EXTRACTOR", False)  # determinism (curated-order precedent)
    from agent_tools.registry import mcp_entries
    from mcp_gateway.bootstrap import _build_instructions
    from mcp_gateway.schemas import build_tool_list

    read_list, mutating_list = _instruction_tool_lists(
        await _build_instructions(scope_root="", root_title=""),
    )
    enforcement_mutating = {e.name for e in mcp_entries() if e.mutating}

    for name in (t.name for t in build_tool_list()):
        if name == "init":
            assert name not in read_list, "init must be prose-only, not in the read list"
            assert name not in mutating_list, "init must be prose-only, not in the mutating list"
            continue
        expected_mutating = name in enforcement_mutating
        assert (name in mutating_list) == expected_mutating, (
            f"{name}: instructions classify it as "
            f"{'mutating' if name in mutating_list else 'read'} but server.call_tool "
            f"enforces it as {'mutating' if expected_mutating else 'read'}"
        )
        # A non-mutating tool must land in the read list, and never both.
        if expected_mutating:
            assert name in mutating_list and name not in read_list, name
        else:
            assert name in read_list and name not in mutating_list, name


async def test_attach_file_advertised_as_mutating_not_read():
    """attach_file's EFFECT is a write (it mints a URL that performs an
    unauthenticated write at redeem), so it must appear in the init instructions'
    MUTATING list — NOT the read list. A read-only key reading the instructions
    top-to-bottom must learn it cannot use it, instead of trying and hitting a 403
    that reads like the transport is broken."""
    from mcp_gateway.bootstrap import _build_instructions

    read_list, mutating_list = _instruction_tool_lists(
        await _build_instructions(scope_root="", root_title=""),
    )
    assert "attach_file" in mutating_list
    assert "attach_file" not in read_list
    # D7: reprocess_file wipes content + queues a re-run — also a write.
    assert "reprocess_file" in mutating_list


# ─── Auth gates ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_missing_auth_rejected_at_asgi_gate(client, mcp_running):
    """Missing Authorization header ⇒ 401 at the cheap ASGI gate (before the
    session manager runs)."""
    resp = await client.post("/mcp", json=_rpc("tools/list"))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mcp_malformed_auth_rejected(client, mcp_running):
    """Authorization not a Bearer token ⇒ 401 at the ASGI gate."""
    resp = await client.post(
        "/mcp", json=_rpc("tools/list"),
        headers={"Authorization": "Basic xyz"},
    )
    assert resp.status_code == 401


# ─── HTTP method gate (stateless ⇒ no standalone GET SSE stream) ──────────────


@pytest.mark.asyncio
async def test_mcp_get_sse_stream_rejected_405(client, mcp_running):
    """GET /mcp (the SDK's standalone server→client SSE stream) is rejected with
    405 promptly in stateless mode — it must NOT hang (the SDK would otherwise
    open an unbounded keep-alive stream that trips the prod proxy read-timeout)."""
    resp = await client.get(
        "/mcp", headers={"Authorization": "Bearer lore_" + "f" * 64,
                         "Accept": "text/event-stream"},
    )
    assert resp.status_code == 405
    assert "not supported" in resp.text


@pytest.mark.asyncio
async def test_mcp_get_405_before_auth(client, mcp_running):
    """GET-405 fires before the auth gate (method-not-allowed is not
    credential-dependent) — no token still yields 405, not 401."""
    resp = await client.get("/mcp", headers={"Accept": "text/event-stream"})
    assert resp.status_code == 405


@pytest.mark.asyncio
async def test_mcp_post_still_works_after_get_gate(client, mcp_running, test_db, admin_user, project_with_doc):
    """Regression guard: the GET gate does not affect POST dispatch."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)
    resp = await client.post("/mcp", json=_rpc("tools/list"), headers=_mcp_headers(token))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_mcp_delete_still_405(client, mcp_running):
    """DELETE /mcp remains 405 (unchanged SDK behaviour)."""
    resp = await client.request(
        "DELETE", "/mcp", headers={"Authorization": "Bearer lore_" + "f" * 64},
    )
    assert resp.status_code == 405


# ─── read_document ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_document_cross_project_is_uniform_not_found(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A doc id from ANOTHER project must yield a uniform not-found error (no
    existence oracle). Parity with the Tool-API uniform-404."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    # "cross-project" id — does not belong to this project at all.
    data = await _mcp_call(client, agent_tok, "read_document", {"document_id": "doc-not-in-project"})
    assert _is_error(data)
    payload = _result_text(data)
    # Uniform not-found: the detail must not leak whether the doc exists elsewhere.
    assert payload["status_code"] == 404


@pytest.mark.asyncio
async def test_read_document_out_of_scope_is_403_naming_root(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A read_document on a doc OUTSIDE the key's subtree is a 403 (not a uniform
    404) whose detail names the scope root — the recovery the agent needs.

    Pins the `_raise_for_soft_error` text-matching trap: the out-of-scope wording
    must NOT contain 'not found' / 'not in this project', or a scope 403 would be
    mis-mapped to a uniform 404 and lose the recovery signal."""
    pid, root_doc, admin_uid = project_with_doc
    _, admin_token = admin_user
    # An out-of-scope doc: a separate top-level doc (not a descendant of root_doc).
    other_doc = await _make_doc(client, admin_token, pid, "Elsewhere", "x")
    scoped_tok = await _make_scoped_agent_key(test_db, admin_uid, pid, root_doc)

    data = await _mcp_call(client, scoped_tok, "read_document", {"document_id": other_doc})
    assert _is_error(data)
    payload = _result_text(data)
    assert payload["status_code"] == 403, payload
    # The detail names the scope root so the agent can re-target in-scope.
    assert root_doc in payload["error"]


# ─── Parity with Tool-API HTTP responses ──────────────────────────────────────


@pytest.mark.asyncio
async def test_search_materials_parity(client, mcp_running, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    await _make_doc(client, admin_token, pid, "Dragons", "Scaly winged reptiles")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    args = {"query": "Dragons", "k": 5}
    data = await _mcp_call(client, agent_tok, "search_materials", args)
    mcp_hits = _result_text(data)

    # Tool-API HTTP parity.
    resp = await client.post("/api/tool/search_materials", json=args, headers=_hdr(agent_tok))
    assert resp.status_code == 200
    assert mcp_hits == resp.json()
