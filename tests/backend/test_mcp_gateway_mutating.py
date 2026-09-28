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
async def test_edit_document_readwrite_applies_with_checkpoint(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A read-write key's edit applies directly (never proposed) and leaves a
    pre-edit checkpoint."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Bestiary", "Goblins are weak.")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": doc_id, "old_string": "weak", "new_string": "fierce",
    })
    assert not _is_error(data)
    assert _result_text(data)["status"] == "applied"

    read = await _mcp_call(client, agent_tok, "read_document", {"document_id": doc_id})
    assert "fierce" in _result_text(read)["content"]

    from db import get_db
    db = await get_db()
    cps = await db.query("SELECT id FROM checkpoints WHERE document_id = $d", {"d": doc_id})
    assert cps, "expected a pre-edit checkpoint after a read-write edit"


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


@pytest.mark.asyncio
async def test_create_document_readwrite_applies(client, mcp_running, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "create_document", {"title": "Applied Doc", "content": "Body", "parent_id": None})
    payload = _result_text(data)
    assert payload["status"] == "applied"
    assert "doc_id" in payload


# ─── read-only key: mutating tools rejected at dispatch (403) ─────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("edit_document", {"document_id": "d", "old_string": "a", "new_string": "b"}),
    ("create_document", {"title": "X", "content": "y", "parent_id": None}),
    ("edit_table_cell", {"document_id": "d", "table_id": "t", "row": 0, "column": "H",
                         "old_value": "a", "new_value": "b"}),
])
async def test_readonly_key_rejects_mutating(
    client, mcp_running, test_db, admin_user, project_with_doc, name, args,
):
    """A read-only key (auto_apply falsy) is rejected with 403 on every mutating
    tool BEFORE any target resolution — the binary key model."""
    pid, doc_id, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=False)
    # Point the edit tools at a real in-project doc so the 403 is the read-only
    # gate, not a 404.
    if "document_id" in args:
        args = {**args, "document_id": doc_id}
    data = await _mcp_call(client, agent_tok, name, args)
    assert _is_error(data), f"{name} must be rejected on a read-only key"
    payload = _result_text(data)
    assert payload["status_code"] == 403
    assert "read-only" in payload["error"].lower()


@pytest.mark.asyncio
async def test_readonly_key_still_reads(client, mcp_running, test_db, admin_user, project_with_doc):
    """A read-only key can still read (only mutating tools are gated)."""
    pid, doc_id, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=False)
    read = await _mcp_call(client, agent_tok, "read_document", {"document_id": doc_id})
    assert not _is_error(read)


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
async def test_create_document_reference_via_node_type(client, mcp_running, test_db, admin_user, project_with_doc):
    """D3 (plan agent-document-placement-and-node-type): a leaf is created via
    node_type="reference" + parent_id (the host). is_reference is no longer an
    argument — the server derives it from node_type. A leaf attaches to its host
    and reads back is_reference=true."""
    from db import fetch_one

    pid, doc_id, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)
    data = await _mcp_call(client, agent_tok, "create_document", {
        "title": "Leaf", "content": "x", "parent_id": doc_id,
        "node_type": "reference",
    })
    assert not _is_error(data), data
    leaf_id = _result_text(data).get("document_id") or _result_text(data).get("doc_id")
    row = await fetch_one("documents", leaf_id)
    assert row["is_reference"] is True
    assert row["parent_id"] == doc_id


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


# ─── error results carry next_action (ergonomics #4) ──────────────────────────


@pytest.mark.asyncio
async def test_error_result_carries_next_action(client, mcp_running, test_db, admin_user, project_with_doc):
    """Every MCP error result carries an explicit next_action instruction."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)
    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": "doc-not-in-project", "old_string": "a", "new_string": "b",
    })
    payload = _result_text(data)
    assert payload["status_code"] == 404
    # 404 = not found: re-reading the same id can't succeed → verify it (409 is the
    # "re-read then retry" case, where the doc exists but old_string drifted).
    assert payload["next_action"] == "verify the id"


# ─── mutating tool descriptions state the always-apply contract ───────────────


@pytest.mark.asyncio
async def test_mutating_tool_descriptions_state_no_confirmation(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Every mutating tool description over MCP states the direct-apply contract
    (applies immediately, reversible in History, read-only key → 403) and carries
    NO proposal/confirmation vocabulary (the shared AGENT_TOOLS wording is replaced,
    not suffixed)."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/mcp", json=_rpc("tools/list"), headers=_mcp_headers(agent_tok))
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}
    # Scope to what the gateway actually SERVES: MUTATING_TOOLS is a superset — it also
    # carries Pi-only tools (sandbox_bash) that build_tool_list never advertises, so
    # iterating it raw would KeyError on a tool that is absent here BY DESIGN. Same
    # reasoning as schemas._GATEWAY_SERVED_MUTATING; test_sandbox_tool asserts the
    # absence itself.
    from agent.tools import AGENT_TOOLS, MUTATING_TOOLS

    gateway_mutating = MUTATING_TOOLS & {s["function"]["name"] for s in AGENT_TOOLS}
    assert gateway_mutating, "the gateway must still serve mutating tools"

    for name in gateway_mutating:
        desc = tools[name]["description"]
        assert "applies immediately" in desc, f"{name} missing direct-apply sentence"
        assert "Lore History panel" in desc, f"{name} missing reversibility note"
        lowered = desc.lower()
        for banned in ("proposed", "confirmation", "awaiting approval", "ignore"):
            assert banned not in lowered, f"{name} description still contains '{banned}'"


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


# ─── Structured logging of MCP calls ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_call_emits_structured_log(
    client, mcp_running, test_db, admin_user, project_with_doc, caplog,
):
    import logging as _logging

    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Log Doc", "Some content.")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    with caplog.at_level(_logging.INFO, logger="mcp_gateway.server"):
        data = await _mcp_call(client, agent_tok, "read_document", {"document_id": doc_id})
    assert not _is_error(data)

    calls = [r for r in caplog.records if r.getMessage() == "mcp.call"]
    assert len(calls) == 1, f"expected exactly one mcp.call record, got {len(calls)}"
    rec = calls[0]
    assert rec.tool == "read_document"
    assert getattr(rec, "key_id", None), "key_id missing from mcp.call record"
    assert getattr(rec, "project_id", None) == pid


# ─── Mutating-write rate limit for external agents ────────────────────────────


@pytest.mark.asyncio
async def test_mutating_rate_limit_trips_and_reads_unaffected(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """30 mutating writes / 60s per agent key. The 31st mutating call returns an
    MCP isError result with status_code 429; read-only calls remain unaffected."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Limit Test", "content")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    for i in range(30):
        data = await _mcp_call(client, agent_tok, "create_document", {"title": f"Doc {i}", "content": f"body {i}", "parent_id": None})
        assert not _is_error(data), f"mutating call {i} unexpectedly failed: {data}"

    data = await _mcp_call(client, agent_tok, "create_document", {"title": "Over", "content": "limit", "parent_id": None})
    assert _is_error(data), "31st mutating call should have been rate-limited"
    assert _result_text(data)["status_code"] == 429

    read = await _mcp_call(client, agent_tok, "read_document", {"document_id": doc_id})
    assert not _is_error(read), "read_document must not be blocked by the mutating tier"


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
async def test_transport_rate_limit_keyed_per_token(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """F3 (F4 parity): the transport bucket is keyed on the FULL token hash, so a
    second key gets its own bucket and is not starved by the first's spam."""
    import rate_limit as _rl

    monkeypatch.setattr(_rl._tiers["mcp_transport"], "max", 2)

    pid, _, admin_uid = project_with_doc
    tok_a = await _make_agent_key(test_db, admin_uid, pid)
    tok_b = await _make_agent_key(test_db, admin_uid, pid)

    # Exhaust token A's bucket.
    for _ in range(2):
        assert (await client.post(
            "/mcp", json=_rpc("tools/list"), headers=_mcp_headers(tok_a),
        )).status_code == 200
    assert (await client.post(
        "/mcp", json=_rpc("tools/list"), headers=_mcp_headers(tok_a),
    )).status_code == 429

    # Token B still has its full budget (independent bucket).
    resp = await client.post("/mcp", json=_rpc("tools/list"), headers=_mcp_headers(tok_b))
    assert resp.status_code == 200


# ─── batch edit_document — one atomic call carries N pointwise edits ─────────


@pytest.mark.asyncio
async def test_edit_document_batch_applies_atomically_one_checkpoint(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A batch of independent edits applies in ONE call: both changes land and
    exactly one pre-edit checkpoint is recorded."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(
        client, admin_token, pid, "Multi", "alpha beta gamma delta",
    )
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": doc_id,
        "edits": [
            {"old_string": "alpha", "new_string": "ALPHA"},
            {"old_string": "gamma", "new_string": "GAMMA"},
        ],
    })
    assert not _is_error(data), data
    payload = _result_text(data)
    assert payload["status"] == "applied"
    assert payload["applied"] == 2
    assert payload["skipped"] == []

    read = await _mcp_call(client, agent_tok, "read_document", {"document_id": doc_id})
    assert _result_text(read)["content"] == "ALPHA beta GAMMA delta"

    from db import get_db
    db = await get_db()
    cps = await db.query("SELECT id FROM checkpoints WHERE document_id = $d", {"d": doc_id})
    assert len(cps) == 1, "expected exactly ONE pre-edit checkpoint for the batch"


@pytest.mark.asyncio
async def test_edit_document_batch_rejects_whole_on_failure_with_index(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A failing edit rejects the WHOLE batch (nothing applied) and names the
    failing index."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    original = "alpha beta gamma"
    doc_id = await _make_doc(client, admin_token, pid, "Fail", original)
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": doc_id,
        "edits": [
            {"old_string": "alpha", "new_string": "ALPHA"},
            {"old_string": "nonexistent", "new_string": "X"},
        ],
    })
    assert _is_error(data)
    payload = _result_text(data)
    assert payload["status_code"] == 409
    # The detail dict {index, error} is nested under MCP's `error` field.
    assert payload["error"]["index"] == 1

    # Nothing applied — the doc is still the original.
    read = await _mcp_call(client, agent_tok, "read_document", {"document_id": doc_id})
    assert _result_text(read)["content"] == original


@pytest.mark.asyncio
async def test_edit_document_batch_idempotent_skip_returns_skipped_list(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Re-sending an already-applied edit returns applied:0 + a skipped list
    (NOT a not_found error); no checkpoint row is created."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Idem", "the QUICK fox")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": doc_id,
        "edits": [{"old_string": "quick", "new_string": "QUICK"}],
    })
    assert not _is_error(data)
    payload = _result_text(data)
    assert payload["status"] == "applied"
    assert payload["noop"] is True
    assert payload["applied"] == 0
    assert payload["skipped"] == [{"index": 0, "at_cp": 4, "reason": "already_applied"}]

    # No checkpoint row created for a no-op batch.
    from db import get_db
    db = await get_db()
    cps = await db.query("SELECT id FROM checkpoints WHERE document_id = $d", {"d": doc_id})
    assert not cps, "no checkpoint for an all-skipped batch"


@pytest.mark.asyncio
async def test_edit_document_legacy_shape_still_applies_over_mcp(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Shim regression (audit F2): a legacy {document_id, old_string, new_string}
    call (no edits[]) still applies over the MCP surface."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Legacy", "hello world")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": doc_id, "old_string": "hello", "new_string": "HI",
    })
    assert not _is_error(data)
    assert _result_text(data)["status"] == "applied"

    read = await _mcp_call(client, agent_tok, "read_document", {"document_id": doc_id})
    assert _result_text(read)["content"] == "HI world"


@pytest.mark.asyncio
async def test_edit_document_legacy_shape_still_applies_over_tool_api(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Shim regression (audit F2): the legacy shape also works on the in-app
    /api/tool/edit_document HTTP route."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "LegacyHTTP", "foo bar")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    resp = await client.post(
        "/api/tool/edit_document",
        json={"document_id": doc_id, "old_string": "foo", "new_string": "FOO",
              "apply": "auto"},
        headers=_hdr(agent_tok),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"
