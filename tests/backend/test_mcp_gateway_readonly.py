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


async def _add_member(test_db, project_id: str, user_id: str, level: str) -> None:
    pm_id = f"mcp-pm-{project_id}-{user_id}"
    await test_db.query(
        "DELETE type::record('project_members', $id)", {"id": pm_id},
    )
    await test_db.query(
        "CREATE type::record('project_members', $id) SET project_id = $pid, "
        "user_id = $uid, access_level = $lvl",
        {"id": pm_id, "pid": project_id, "uid": user_id, "lvl": level},
    )


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


@pytest.mark.asyncio
async def test_tools_list_deleted_scope_root_renders_empty_title(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A scope root deleted after the key was minted still gets a tools/list —
    the fill renders an empty title (parity with init's capabilities.scope
    today), not an error."""
    pid, root_doc, admin_uid = project_with_doc
    scoped = await _make_scoped_agent_key(test_db, admin_uid, pid, root_doc)
    await test_db.query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": root_doc},
    )

    data = await _mcp_list(client, scoped)
    tools = {t["name"]: t for t in data["result"]["tools"]}
    expected = f'the "" subtree root ({root_doc})'
    assert expected in tools["move_document"]["description"]
    assert expected in (
        tools["move_document"]["inputSchema"]["properties"]["parent_id"]["description"]
    )


@pytest.mark.asyncio
async def test_tools_list_unscoped_key_pins_the_root_wording(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """An UNSCOPED key's tools/list keeps 'the project root' — true there (the
    executor keeps null ⇒ project root for unscoped keys). Pinned post-{{ROOT}}:
    the fill renders the constant; move_document's MCP text is normalized by it
    ('null = project root' → 'null = the project root', the plan's one
    deliberate byte-identity exception)."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    data = await _mcp_list(client, token)
    tools = {t["name"]: t for t in data["result"]["tools"]}
    assert "null = the project root), or the HOST" in tools["create_document"]["description"]
    assert "parent_id = new parent (null = the project root);" in tools["move_document"]["description"]
    assert "null for the project root." in (
        tools["move_document"]["inputSchema"]["properties"]["parent_id"]["description"]
    )
    for which, text in _every_served_text(data["result"]["tools"]):
        assert "{{ROOT}}" not in text, which


@pytest.mark.asyncio
async def test_tools_list_rejects_a_bad_token(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """tools/list authenticates like tools/call: a well-formed but INVALID Bearer
    is a JSON-RPC error carrying the 401 detail — never a silent fallback to the
    unscoped list (the wording this render exists to serve)."""
    resp = await client.post(
        "/mcp",
        json=_rpc("tools/list"),
        headers=_mcp_headers("lore_" + secrets.token_hex(32)),
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert "result" not in payload, payload
    assert payload["error"]["data"]["status_code"] == 401, payload
    assert "Invalid API key" in payload["error"]["message"], payload


@pytest.mark.asyncio
async def test_tools_list_rejects_a_revoked_member(
    client, mcp_running, test_db, admin_user, regular_user, project_with_doc,
):
    """The failure actor: a key whose owning user was removed from the project is
    refused on tools/list with the live-RBAC 403 detail reaching the client."""
    pid, _, _ = project_with_doc
    reg_uid, _ = regular_user
    await _add_member(test_db, pid, reg_uid, "full")
    agent_tok = await _make_agent_key(test_db, reg_uid, pid)
    await test_db.query(
        "DELETE type::record('project_members', $id)",
        {"id": f"mcp-pm-{pid}-{reg_uid}"},
    )

    resp = await client.post("/mcp", json=_rpc("tools/list"), headers=_mcp_headers(agent_tok))
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert "result" not in payload, payload
    assert payload["error"]["data"]["status_code"] == 403, payload
    assert "no longer has project access" in payload["error"]["message"], payload


# ─── tools/list + schema anti-drift ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_tools_list_returns_agent_surface(client, mcp_running, test_db, admin_user, project_with_doc):
    """tools/list exposes the Tool-API tool surface verbatim, and read-tool
    schemas deep-equal AGENT_TOOLS parameters (anti-drift guard)."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    data = await _mcp_list(client, token)
    tools = {t["name"]: t for t in data["result"]["tools"]}

    from agent.tools import AGENT_TOOLS
    expected_names = {t["function"]["name"] for t in AGENT_TOOLS}
    assert expected_names <= set(tools), (
        f"missing tools: {expected_names - set(tools)}"
    )
    # Plan tool-surface-consolidation Step 2a: list_references was folded into
    # get_project_structure (which now returns media_type/source_url on ref rows).
    # It is intentionally absent from the advertised surface.
    assert "list_references" not in tools

    # Anti-drift: the read-tool inputSchemas must equal AGENT_TOOLS parameters.
    read_in_agent = {
        t["function"]["name"]: t for t in AGENT_TOOLS
        if t["function"]["name"] in {
            "search_materials", "read_document", "get_project_structure",
        }
    }
    for name, spec in read_in_agent.items():
        expected = spec["function"]["parameters"]
        # D8 (plan mcp-tool-surface-redesign): read_document's `required:
        # ["document_id"]` is now RESTORED — the name fallback is gone (titles are
        # not unique, so a name is not an address), so the SDK enforces id-only at
        # validation. No transform needed: the MCP schema matches AGENT_TOOLS verbatim.
        assert tools[name]["inputSchema"] == expected, (
            f"{name} schema drifted from AGENT_TOOLS"
        )


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
        "create_document", "edit_document", "append_to_document",
        "attach_file", "get_file", "reprocess_file",
        "move_document",
        "create_table", "edit_table_cell", "add_table_rows", "add_table_column",
        # rename_document: not in _TOOL_LIST_ORDER (deliberately unordered —
        # curation is cosmetic), so build_tool_list APPENDS it.
        "rename_document",
        # D14: import_file is Pi-only now (MCP serves attach_file). 16 tools by default.
    ], names


def test_read_tools_carry_real_output_schema():
    """The three read tools advertise a real per-tool outputSchema (not the generic
    result union), so a weak model learns the shape without a probe call. Step 2c
    folded read_table into read_document — its `tables` field is now a property of
    read_document's outputSchema."""
    from mcp_gateway.schemas import _AGENT_TOOL_OUTPUT_SCHEMA, build_tool_list

    tools = {t.name: t for t in build_tool_list()}
    assert "content" in tools["read_document"].output_schema["properties"]
    # Step 2c: read_table's tables field folded into read_document.
    assert "tables" in tools["read_document"].output_schema["properties"]
    assert "hits" in tools["search_materials"].output_schema["properties"]
    assert "documents" in tools["get_project_structure"].output_schema["properties"]
    # read_table is no longer a standalone tool.
    assert "read_table" not in tools
    # Mutating tools keep the generic union.
    assert tools["edit_document"].output_schema == _AGENT_TOOL_OUTPUT_SCHEMA


def test_search_materials_hit_advertises_kind_and_sources():
    """A `memory` hit carries `sources` (the refs a fact was distilled from) and a
    `kind` label; both MUST be in the advertised outputSchema so a weak model learns,
    from tools/list alone, that `sources` is the route to the raw material (D1: the
    agent otherwise brute-forces search for a passage it already holds a pointer to)."""
    from mcp_gateway.schemas import build_tool_list

    tools = {t.name: t for t in build_tool_list()}
    hit_props = tools["search_materials"].output_schema["properties"]["hits"]["items"]["properties"]
    assert "kind" in hit_props
    assert "sources" in hit_props


def test_edit_table_cell_advertises_column_only():
    """edit_table_cell's MCP inputSchema advertises the batch edits[] shape; each edit
    item carries `column` (header name) and NEVER `col` (numeric index) — `col` stays a
    tolerated dispatch-only fallback, absent from the advertised schema entirely."""
    from mcp_gateway.schemas import build_tool_list

    tools = {t.name: t for t in build_tool_list()}
    schema = tools["edit_table_cell"].input_schema
    # top-level: document_id + edits only (no col, no flat table_id/row)
    assert set(schema["properties"].keys()) == {"document_id", "edits"}
    item = schema["properties"]["edits"]["items"]
    assert "column" in item["properties"]
    assert "col" not in item["properties"]
    assert "column" in item["required"]


def test_edit_document_advertises_edits_array_only():
    """edit_document's MCP inputSchema advertises `edits` (list of {old_string,
    new_string}) ONLY — the legacy singular old_string/new_string stay unadvertised
    (tolerated by dispatch as a back-compat shim). Mirrors the edit_table_cell/
    `col` precedent."""
    from mcp_gateway.schemas import build_tool_list

    tools = {t.name: t for t in build_tool_list()}
    props = tools["edit_document"].input_schema["properties"]
    assert "edits" in props
    # The legacy singular form is NOT advertised (tolerated by dispatch only).
    assert "old_string" not in props
    assert "new_string" not in props
    # edits is an array of {old_string, new_string}.
    items = props["edits"]["items"]
    assert items["type"] == "object"
    assert set(items["properties"]) >= {"old_string", "new_string"}
    # `edits` is advertised but NOT required on MCP — the legacy singular form is
    # tolerated by dispatch (coalesced to one edit). The SDK enforces `required`
    # before dispatch, so requiring `edits` would 400 a legacy call. Same projection
    # as edit_table_cell's `col` / read_document's `required`-strip.
    assert tools["edit_document"].input_schema.get("required") == ["document_id"]


# ─── advertised-vs-routed guard ──────────────────────────────────────────────


def test_every_advertised_tool_is_routed():
    """Advertised (tools/list) names must all be routable over MCP.

    # ARCH (plan "mcp-gateway-debt-paydown", Decision 3): the advertised surface
    # (schemas.build_tool_list) and the routed surface (dispatch_tool's served
    # set) both read the ONE registry — the two-maintained-places era is gone;
    # this guard now pins that neither side re-introduces a second table.
    """
    from mcp_gateway.schemas import _served_mcp_names, build_tool_list

    advertised = {t.name for t in build_tool_list()}
    routed = _served_mcp_names()  # what dispatch_tool serves (same predicate)
    unrouted = advertised - routed
    assert not unrouted, (
        f"advertised tools with no dispatch path (would 404 at call time): {unrouted}"
    )
    unadvertised = routed - advertised
    assert not unadvertised, (
        f"routed tools not advertised in tools/list: {unadvertised}"
    )


def test_create_reference_intentionally_absent_from_mcp_surface():
    """create_reference is NOT routed over MCP — the unified create_document
    (is_reference=True) surface replaces it for new Pi/MCP calls.

    # ARCH (plan "mcp-gateway-debt-paydown"): a NEGATIVE assertion, not a gap to
    # close. create_reference is absent from both AGENT_TOOLS and the gateway-only
    # tool set, so it is neither advertised nor routed. This pins that contract so
    # a future slice cannot silently resurrect the deprecated surface over MCP.
    """
    from agent_tools.registry import REGISTRY
    from mcp_gateway.schemas import _served_mcp_names, build_tool_list

    advertised = {t.name for t in build_tool_list()}
    routed = _served_mcp_names()
    assert "create_reference" not in advertised
    assert "create_reference" not in routed
    assert "create_reference" not in REGISTRY


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


@pytest.mark.asyncio
async def test_mcp_invalid_key_on_call_is_mcp_error(client, mcp_running):
    """Format-valid but non-existent key passes the ASGI gate, but the per-call
    auth check inside call_tool rejects it as an MCP tool error (isError)."""
    resp = await client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "read_document", "arguments": {"document_id": "x"}}),
        headers=_mcp_headers("lore_" + "f" * 64),
    )
    assert resp.status_code == 200
    assert _is_error(resp.json())


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
async def test_read_document_happy_path(client, mcp_running, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "ReadMe", "# Hello world")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    data = await _mcp_call(client, agent_tok, "read_document", {"document_id": doc_id})
    assert not _is_error(data)
    payload = _result_text(data)
    assert payload["doc_id"] == doc_id
    assert "Hello world" in payload["content"]


def test_read_document_schema_advertises_slice_window():
    """read_document's offset/limit params and
    the truncation metadata (offset/total_chars/next_offset) MUST be advertised —
    a weak model learns the paging contract from tools/list alone, not from a
    first truncated probe."""
    from mcp_gateway.schemas import build_tool_list

    tools = {t.name: t for t in build_tool_list()}
    props = tools["read_document"].input_schema["properties"]
    assert "offset" in props and props["offset"].get("minimum") == 0
    assert "limit" in props and props["limit"].get("minimum") == 1
    out_props = tools["read_document"].output_schema["properties"]
    for field in ("offset", "total_chars", "next_offset"):
        assert field in out_props, (
            f"read_document outputSchema must advertise {field} — an external "
            "agent otherwise meets an undeclared shape on a truncated read"
        )


@pytest.mark.asyncio
async def test_read_document_slice_over_mcp(client, mcp_running, test_db, admin_user, project_with_doc):
    """offset/limit flow through the MCP dispatch; a non-integer window is
    refused by the gateway's inputSchema validation before dispatch (unlike `mode`,
    which the dispatch coerces — a window has no safe fallback value)."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Sliced", "A" * 40 + "B" * 40)
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    data = await _mcp_call(client, agent_tok, "read_document", {
        "document_id": doc_id, "limit": 50,
    })
    assert not _is_error(data)
    p1 = _result_text(data)
    assert p1["content"] == "A" * 40 + "B" * 10
    assert p1["offset"] == 0
    assert p1["total_chars"] == 80
    assert p1["next_offset"] == 50

    # A non-integer window is rejected by the gateway's inputSchema validation BEFORE
    # dispatch — a clean, self-correctable MCP error naming the constraint (the
    # HTTP path 422s the same shape; the executor's clamp covers what a schema
    # CANNOT validate: offset past end, limit over the hard max).
    data = await _mcp_call(client, agent_tok, "read_document", {
        "document_id": doc_id, "offset": "50",
    })
    assert _is_error(data)
    assert "integer" in data["result"]["content"][0]["text"]

    data = await _mcp_call(client, agent_tok, "read_document", {
        "document_id": doc_id, "offset": 50,
    })
    assert not _is_error(data)
    p2 = _result_text(data)
    assert p2["content"] == "B" * 30
    assert p2["offset"] == 50
    assert "next_offset" not in p2


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


@pytest.mark.asyncio
async def test_read_document_revoked_membership_blocks(
    client, mcp_running, test_db, admin_user, regular_user, project_with_doc,
):
    """A key issued to a since-removed member is rejected by the live RBAC
    re-check (per-call project-membership verification)."""
    pid, _, admin_uid = project_with_doc
    reg_uid, _ = regular_user
    await _add_member(test_db, pid, reg_uid, "full")
    agent_tok = await _make_agent_key(test_db, reg_uid, pid)

    # Revoke membership.
    await test_db.query(
        "DELETE type::record('project_members', $id)",
        {"id": f"mcp-pm-{pid}-{reg_uid}"},
    )

    data = await _mcp_call(client, agent_tok, "read_document", {"document_id": "any"})
    assert _is_error(data)
    payload = _result_text(data)
    # No project access ⇒ rejected (live RBAC re-check), not 200.
    assert payload["status_code"] in (401, 403)


async def _revoke_membership(test_db, project_id: str, user_id: str) -> None:
    """Delete the project_members row — the live RBAC re-check then refuses."""
    await test_db.query(
        "DELETE type::record('project_members', $id)",
        {"id": f"mcp-pm-{project_id}-{user_id}"},
    )


@pytest.mark.asyncio
async def test_revoked_key_refusal_envelope_on_first_call(
    client, mcp_running, test_db, admin_user, regular_user, project_with_doc,
):
    """A revoked key's tools/call refusal keeps the JSON envelope
    {error, status_code, next_action} on the key's very first call.

    Why: a refusal produced anywhere but _call_tool (an SDK-side schema
    refresh through the list handler) comes back as bare text without
    status_code/next_action (the run #1259 flake)."""
    pid, _, admin_uid = project_with_doc
    reg_uid, _ = regular_user
    await _add_member(test_db, pid, reg_uid, "full")
    agent_tok = await _make_agent_key(test_db, reg_uid, pid)
    await _revoke_membership(test_db, pid, reg_uid)

    data = await _mcp_call(client, agent_tok, "read_document", {"document_id": "any"})
    assert _is_error(data)
    payload = _result_text(data)
    assert payload["status_code"] in (401, 403), payload
    assert payload["next_action"] == "stop", payload


@pytest.mark.asyncio
async def test_revoked_key_refusal_envelope_unknown_tool(
    client, mcp_running, test_db, admin_user, regular_user, project_with_doc,
):
    """Same envelope guarantee for a tool name the registry never served."""
    pid, _, admin_uid = project_with_doc
    reg_uid, _ = regular_user
    await _add_member(test_db, pid, reg_uid, "full")
    agent_tok = await _make_agent_key(test_db, reg_uid, pid)
    await _revoke_membership(test_db, pid, reg_uid)

    data = await _mcp_call(client, agent_tok, "no_such_tool", {})
    assert _is_error(data)
    payload = _result_text(data)
    assert payload["status_code"] in (401, 403), payload
    assert payload["next_action"] == "stop", payload


@pytest.mark.asyncio
async def test_bad_token_schema_invalid_call_is_refused_by_auth_not_schema(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Input validation runs AFTER auth: an invalid key sending schema-invalid
    arguments gets the 401, never the 422 that names the violated constraint —
    an unauthenticated caller cannot probe a tool's inputSchema."""
    data = await _mcp_call(
        client, "lore_" + secrets.token_hex(32), "read_document",
        {"document_id": "any", "offset": "50"},
    )
    assert _is_error(data)
    payload = _result_text(data)
    assert payload["status_code"] == 401, payload
    assert "integer" not in json.dumps(payload), payload


@pytest.mark.asyncio
async def test_revoked_member_probe_does_not_stamp_last_used(
    client, mcp_running, test_db, admin_user, regular_user, project_with_doc,
):
    """A probe from a since-removed member is rejected by the live RBAC re-check,
    and that rejected probe must NOT register as a use (last_used_at stays
    untouched). Why: stamping last_used_at on a rejected probe would keep a
    revoked member's automated probe looking 'active' in the key list — a stale
    signal. The stamp belongs AFTER the liveness check, so only successful
    resolutions count as use.

    Plan "mcp-gateway-debt-paydown" Decision 7.
    """
    from db import get_db

    pid, _, _ = project_with_doc
    reg_uid, _ = regular_user
    await _add_member(test_db, pid, reg_uid, "full")
    agent_tok = await _make_agent_key(test_db, reg_uid, pid)

    # Revoke membership BEFORE any successful use ⇒ the very first probe is a
    # rejected probe (so last_used_at must never have been written).
    await test_db.query(
        "DELETE type::record('project_members', $id)",
        {"id": f"mcp-pm-{pid}-{reg_uid}"},
    )

    data = await _mcp_call(client, agent_tok, "read_document", {"document_id": "any"})
    assert _is_error(data)

    # The rejected probe must NOT have stamped last_used_at.
    token_hash = hashlib.sha256(agent_tok.encode()).hexdigest()
    db = await get_db()
    rows = await db.query(
        "SELECT last_used_at FROM api_keys WHERE token_hash = $h",
        {"h": token_hash},
    )
    assert rows, "key row not found"
    assert rows[0].get("last_used_at") is None, (
        "rejected probe stamped last_used_at — stamp must run AFTER the liveness check"
    )


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


@pytest.mark.asyncio
async def test_get_project_structure_parity(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    data = await _mcp_call(client, agent_tok, "get_project_structure", {})
    mcp_struct = _result_text(data)

    resp = await client.post("/api/tool/get_project_structure", json={}, headers=_hdr(agent_tok))
    assert resp.status_code == 200
    assert mcp_struct == resp.json()


@pytest.mark.asyncio
async def test_get_project_structure_returns_reference_fields(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Plan tool-surface-consolidation Step 2a: the reference-listing capability
    (media_type/source_url) is folded INTO get_project_structure. A reference row
    in the structure response now carries those fields — the removed
    list_references tool's payload is derivable from this one call."""
    pid, host_doc, admin_uid = project_with_doc
    _, admin_token = admin_user
    # Create a markdown reference under the index doc.
    resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "parent_id": host_doc, "title": "Ref A",
            "media_type": "markdown", "is_reference": True, "content": "ref body",
        },
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    # The root call hides references by default (plan structure-layered-walk);
    # the folded reference-listing capability is reached with the flag — and
    # parity must hold on that shape too.
    args = {"include_references": True}
    data = await _mcp_call(client, agent_tok, "get_project_structure", args)
    mcp_struct = _result_text(data)

    resp = await client.post("/api/tool/get_project_structure", json=args, headers=_hdr(agent_tok))
    assert resp.status_code == 200
    assert mcp_struct == resp.json()
    # The reference row carries the folded fields.
    refs = [d for d in mcp_struct["documents"] if d.get("is_reference")]
    assert refs, "expected the seeded reference in the structure"
    ref = refs[0]
    assert "media_type" in ref
    assert ref["media_type"] == "markdown"
    assert "source_url" in ref


@pytest.mark.asyncio
async def test_read_document_returns_tables_index_by_default(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Plan tool-surface-consolidation Step 2c: read_table folded into read_document.
    A doc with an editable table anchor, read via read_document, returns a `tables`
    index by default (table_id + label + n_cols, no rows). Parity with the Tool-API
    HTTP path."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(
        client, admin_token, pid, "TableDoc",
        "Intro\n\n![Prices](table:t1)\n",
    )
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    data = await _mcp_call(client, agent_tok, "read_document", {"document_id": doc_id})
    mcp_doc = _result_text(data)

    resp = await client.post(
        "/api/tool/read_document", json={"document_id": doc_id}, headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200
    assert mcp_doc == resp.json()
    # The tables index is present (empty for a doc whose anchor has no live grid yet).
    assert "tables" in mcp_doc
    assert mcp_doc["tables"] == []
