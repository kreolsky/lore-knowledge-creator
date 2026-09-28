"""Contract tests for the MCP `preview_extractor` tool (renamed from run_extractor, D13) (plan: mcp-run-extractor-parity).

The tool resolves params EXACTLY like the editor (via resolve_extractor_params)
and runs the flow dry (no document). These tests pin the tool's own contract:
routing, the multi-config disambiguation, the dry-run-only guard, model override,
and that it is advertised in tools/list. The resolver + dry entrypoint are mocked
so the assertions bind the tool wiring (not the LLM/DB).
"""
import hashlib
import json
import secrets
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException


# Plan tool-surface-consolidation Step 2d: run_extractor is gated OFF the default
# MCP surface. These tests exercise an EXPLICITLY-ENABLED deployment (the CIR
# benchmark path the gate preserves), so the flag is turned on for the module.
@pytest.fixture(autouse=True)
def _enable_run_extractor(monkeypatch):
    import config

    monkeypatch.setattr(config, "MCP_RUN_EXTRACTOR", True)


# ─── shared helpers (mirror test_mcp_gateway_readonly) ───────────────────────


async def _make_agent_key(test_db, user_id: str, project_id: str) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"mcp-re-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": "",
        "token_hash": token_hash,
        "label": "preview_extractor",
        "capabilities": ["agent"],
    })
    return token


def _rpc(method: str, params: dict | None = None, *, id_: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def _mcp_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


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


def _payload(resp_json: dict) -> dict:
    result = resp_json["result"]
    return json.loads(result["content"][0]["text"])


def _is_error(resp_json: dict) -> bool:
    return resp_json.get("result", {}).get("isError", False)


def _params(**kw):
    from pipeline.extractor.params import ExtractorParams

    defaults = dict(
        reference_id="ref-1", source_doc_id="src-1", config_doc_id="cfg-1",
        target_doc_id="tgt-1", project_id="p-1", title_template=None, model=None,
    )
    defaults.update(kw)
    return ExtractorParams(**defaults)


# ─── advertisement ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_extractor_advertised(client, mcp_running, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    data = await _mcp_list(client, token)
    tools = {t["name"]: t for t in data["result"]["tools"]}
    assert "preview_extractor" in tools
    schema = tools["preview_extractor"]["inputSchema"]
    assert schema["required"] == ["reference_id"]
    props = set(schema["properties"])
    assert {"reference_id", "dry_run", "config_doc_id", "model"} <= props


# ─── happy path ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_extractor_dry_returns_data(client, mcp_running, test_db, admin_user, project_with_doc):
    """A default (read-only) agent key can dry-run: the tool writes nothing, so it
    is gated on project access (the resolver), NOT on key writability."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    with patch("pipeline.extractor.params.resolve_extractor_params",
               new=AsyncMock(return_value=[_params(project_id=pid, config_doc_id="cfg-1")])) as mk_res, \
            patch("pipeline.extractor.runner.run_extractor_dry",
                  new=AsyncMock(return_value={
                      "extracted_data": {"x": 1},
                      "rendered_markdown": "# x",
                      "variable_duplicates": [],
                  })) as mk_dry:
        data = await _mcp_call(client, token, "preview_extractor", {"reference_id": "ref-1"})

    assert not _is_error(data), data
    payload = _payload(data)
    assert payload["extracted_data"] == {"x": 1}
    assert payload["rendered_markdown"] == "# x"
    # The benchmark uses reference_id to drive one resolution path; the resolver
    # is called with (reference_id, ctx_user) — same user the key authenticates.
    assert mk_res.await_count == 1
    assert mk_res.call_args.args[0] == "ref-1"
    assert mk_res.call_args.args[1]["user_id"] == admin_uid
    mk_dry.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_extractor_echoes_resolved_inputs(client, mcp_running, test_db, admin_user, project_with_doc):
    """The result echoes the resolved inputs (source/config/target/project/model)
    so a parity check can prove the tool ran the same inputs as the editor."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    resolved = _params(project_id=pid, config_doc_id="cfg-echo", model="local/orange/chat")
    with patch("pipeline.extractor.params.resolve_extractor_params",
               new=AsyncMock(return_value=[resolved])), \
            patch("pipeline.extractor.runner.run_extractor_dry",
                  new=AsyncMock(return_value={"extracted_data": {}, "rendered_markdown": "", "variable_duplicates": []})):
        data = await _mcp_call(client, token, "preview_extractor", {"reference_id": "ref-1"})

    payload = _payload(data)
    assert payload["resolved"]["config_doc_id"] == "cfg-echo"
    assert payload["resolved"]["project_id"] == pid
    assert payload["resolved"]["model"] == "local/orange/chat"


# ─── guards ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_extractor_missing_reference_id_is_rejected(
        client, mcp_running, test_db, admin_user, project_with_doc):
    """reference_id is required in the schema, so the gateway rejects an empty
    call with an input-validation error before dispatch (the same gate every
    required-arg tool gets). The handler's own guard is defense-in-depth for a
    direct (non-SDK) caller."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    data = await _mcp_call(client, token, "preview_extractor", {})
    assert _is_error(data)
    # The validation refusal names the missing argument, so assert
    # the message.
    text = data["result"]["content"][0]["text"]
    assert "reference_id" in text


@pytest.mark.asyncio
async def test_run_extractor_multiple_configs_requires_disambiguation(
        client, mcp_running, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    two = [_params(project_id=pid, config_doc_id="cfg-a"), _params(project_id=pid, config_doc_id="cfg-b")]
    with patch("pipeline.extractor.params.resolve_extractor_params",
               new=AsyncMock(return_value=two)):
        data = await _mcp_call(client, token, "preview_extractor", {"reference_id": "ref-1"})

    assert _is_error(data)
    assert _payload(data)["status_code"] == 400


@pytest.mark.asyncio
async def test_run_extractor_config_doc_id_disambiguates(
        client, mcp_running, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    two = [_params(project_id=pid, config_doc_id="cfg-a"), _params(project_id=pid, config_doc_id="cfg-b")]
    with patch("pipeline.extractor.params.resolve_extractor_params",
               new=AsyncMock(return_value=two)), \
            patch("pipeline.extractor.runner.run_extractor_dry",
                  new=AsyncMock(return_value={"extracted_data": {}, "rendered_markdown": "", "variable_duplicates": []})) as mk_dry:
        data = await _mcp_call(client, token, "preview_extractor",
                               {"reference_id": "ref-1", "config_doc_id": "cfg-b"})

    assert not _is_error(data), data
    chosen = mk_dry.call_args.args[0]
    assert chosen.config_doc_id == "cfg-b"


@pytest.mark.asyncio
async def test_run_extractor_unknown_config_doc_id_is_404(
        client, mcp_running, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    with patch("pipeline.extractor.params.resolve_extractor_params",
               new=AsyncMock(return_value=[_params(project_id=pid, config_doc_id="cfg-a")])):
        data = await _mcp_call(client, token, "preview_extractor",
                               {"reference_id": "ref-1", "config_doc_id": "nope"})

    assert _is_error(data)
    assert _payload(data)["status_code"] == 404


@pytest.mark.asyncio
async def test_run_extractor_dry_run_false_is_unsupported(
        client, mcp_running, test_db, admin_user, project_with_doc):
    """The tool is dry-run only — a wet run that creates a document is the arq
    path (POST /agent-config/run), not this tool. Refuse explicitly."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    with patch("pipeline.extractor.params.resolve_extractor_params",
               new=AsyncMock(return_value=[_params(project_id=pid)])):
        data = await _mcp_call(client, token, "preview_extractor",
                               {"reference_id": "ref-1", "dry_run": False})

    assert _is_error(data)
    assert _payload(data)["status_code"] == 400


@pytest.mark.asyncio
async def test_run_extractor_model_override(client, mcp_running, test_db, admin_user, project_with_doc):
    """An optional model override replaces the resolved model before the dry run."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    with patch("pipeline.extractor.params.resolve_extractor_params",
               new=AsyncMock(return_value=[_params(project_id=pid, model="local/default")])) , \
            patch("pipeline.extractor.runner.run_extractor_dry",
                  new=AsyncMock(return_value={"extracted_data": {}, "rendered_markdown": "", "variable_duplicates": []})) as mk_dry:
        await _mcp_call(client, token, "preview_extractor",
                        {"reference_id": "ref-1", "model": "local/experimental"})

    chosen = mk_dry.call_args.args[0]
    assert chosen.model == "local/experimental"


# ─── scope confinement (the key's project_id + subtree, like every read tool) ─


@pytest.mark.asyncio
async def test_run_extractor_rejects_cross_project(client, mcp_running, test_db, admin_user, project_with_doc):
    """A key scoped to project A cannot extract from a reference that resolves to
    project B. The agent key's project scope is the trust boundary — not the
    owning user's full membership set (the resolver already checked that)."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    cross = _params(project_id="other-project", config_doc_id="cfg-x")
    with patch("pipeline.extractor.params.resolve_extractor_params",
               new=AsyncMock(return_value=[cross])):
        data = await _mcp_call(client, token, "preview_extractor", {"reference_id": "ref-1"})

    assert _is_error(data)
    assert _payload(data)["status_code"] == 403


@pytest.mark.asyncio
async def test_run_extractor_enforces_subtree_scope(client, mcp_running, test_db, admin_user, project_with_doc):
    """A subtree-scoped key cannot extract from a reference outside its subtree
    (require_doc_in_scope, same wall every read tool applies)."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    with patch("pipeline.extractor.params.resolve_extractor_params",
               new=AsyncMock(return_value=[_params(project_id=pid)])), \
            patch("scope.require_doc_in_scope",
                  new=AsyncMock(side_effect=HTTPException(403, "outside subtree"))):
        data = await _mcp_call(client, token, "preview_extractor", {"reference_id": "ref-1"})

    assert _is_error(data)
    assert _payload(data)["status_code"] == 403
