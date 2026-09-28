"""Slice 1 — result ergonomics: canonical ids + structuredContent/outputSchema.

Two pure-serialization guarantees over the MCP gateway output boundary:
  #2 canonical proposal_id — every id the gateway emits is a plain uuid (no
     SurrealDB record wrapping `table:⟨uuid⟩`). Normalized once at the
     dispatch output boundary, never per-tool.
  #3 structuredContent + outputSchema — `_ok()` emits BOTH the legacy text block
     (back-compat for older clients) AND a `structuredContent` dict; every tool
     declares `Tool.outputSchema` so capable clients get typed results.

Mirrors the parity contract: the text fallback must stay byte-identical so the
readonly/mutating/init/work-product suites stay green (regression guard #4).
"""

import hashlib
import json
import re
import secrets

import pytest

try:
    import jsonschema  # type: ignore
except Exception:  # pragma: no cover — optional validator
    jsonschema = None


# ─── Helpers (shared shape with the other MCP suites) ─────────────────────────


async def _make_agent_key(test_db, user_id: str, project_id: str, *, auto_apply: bool = False) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"mcp-shape-key-{secrets.token_hex(4)}"
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


def _result(resp_json: dict) -> dict:
    """The CallToolResult object off a tools/call response."""
    return resp_json["result"]


def _text_payload(resp_json: dict) -> object:
    return json.loads(_result(resp_json)["content"][0]["text"])


async def _mcp_call(client, token: str, name: str, arguments: dict | None = None) -> dict:
    resp = await client.post(
        "/mcp", json=_rpc("tools/call", {"name": name, "arguments": arguments or {}}),
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


_PLAIN_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _assert_plain_id(value) -> None:
    assert isinstance(value, str), f"id is not a str: {value!r}"
    assert "⟨" not in value and "⟩" not in value, f"id still wrapped: {value!r}"
    assert _PLAIN_UUID.match(value), f"id is not a plain uuid: {value!r}"


# ─── #2 + #3: create_document (applied) round-trips a plain id with typed content


@pytest.mark.asyncio
async def test_create_document_applied_emits_structured_and_plain_id(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """create_document on a read-write key → text parses, structuredContent
    deep-equals it, and doc_id is a plain uuid (no SurrealDB record wrapping)."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    resp = await _mcp_call(client, agent_tok, "create_document", {
        "title": "Shape Doc", "content": "# Hello", "parent_id": None,
    })
    result = _result(resp)

    # Text fallback present (back-compat for older clients).
    text_payload = json.loads(result["content"][0]["text"])
    assert text_payload["status"] == "applied"
    _assert_plain_id(text_payload["doc_id"])

    # structuredContent present and deep-equals the text payload.
    assert result.get("structuredContent") is not None, "structuredContent missing"
    assert result["structuredContent"] == text_payload


# ─── #2b: a nested RecordID in a result is normalized recursively (F5) ─────────


@pytest.mark.asyncio
async def test_nested_record_id_is_canonicalized(monkeypatch):
    """coerce_record_ids coerces a RecordID nested inside a list/dict, not just at
    the top level (F5 audit — type-based recursive normalization). The helper now
    lives in db.py (single record-id coercion, alongside extract_id); the gateway
    output boundary (_ok) calls it."""
    from db import coerce_record_ids

    class _FakeRecordID:
        # Duck-typed to match db.is_record_id's name check.
        def __init__(self, uuid):
            self._uuid = uuid

        def __str__(self):
            return f"documents:`{self._uuid}`"

    _FakeRecordID.__name__ = "RecordID"
    nested = {"documents": [{"id": _FakeRecordID("abc-123"), "title": "x"}], "top": "plain"}
    out = coerce_record_ids(nested)
    assert out["documents"][0]["id"] == "abc-123", "nested RecordID not normalized"
    assert out["top"] == "plain", "a plain string must be untouched"


# ─── #3: every tool declares outputSchema; structuredContent validates against it


@pytest.mark.asyncio
async def test_tools_list_every_tool_has_output_schema(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """tools/list → every tool carries a non-empty outputSchema."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/mcp", json=_rpc("tools/list"), headers=_mcp_headers(agent_tok))
    tools = resp.json()["result"]["tools"]
    assert tools, "tools/list returned no tools"
    for tool in tools:
        schema = tool.get("outputSchema")
        assert isinstance(schema, dict), f"{tool['name']} missing outputSchema"
        assert schema.get("type") == "object", f"{tool['name']} outputSchema not an object"
        assert "properties" in schema, f"{tool['name']} outputSchema has no properties"


def _validate_against_schema(instance: dict, schema: dict, tool_name: str) -> None:
    """Validate structuredContent against the declared outputSchema (jsonschema when
    available; a structural required-keys check otherwise)."""
    if jsonschema is not None:
        jsonschema.validate(instance=instance, schema=schema)
        return
    # Structural fallback: every declared required key is present.
    for key in schema.get("required", []):
        assert key in instance, f"{tool_name} structuredContent missing required '{key}'"


@pytest.mark.asyncio
async def test_structured_content_validates_against_output_schema(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """The structuredContent of an applied create validates against the
    outputSchema tools/list advertised for it."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    resp = await client.post("/mcp", json=_rpc("tools/list"), headers=_mcp_headers(agent_tok))
    schemas = {t["name"]: t["outputSchema"] for t in resp.json()["result"]["tools"]}

    created = await _mcp_call(client, agent_tok, "create_document", {"title": "Validated", "content": "x", "parent_id": None})
    cr = _result(created)
    _validate_against_schema(cr["structuredContent"], schemas["create_document"], "create_document")


# ─── #4: regression — text fallback unchanged (applied path still text-only-safe)


@pytest.mark.asyncio
async def test_applied_result_keeps_text_fallback(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """    An applied (auto) create still returns the legacy text block so older clients
    that ignore structuredContent keep working."""
    pid, _, admin_uid = project_with_doc
    # Slice 3: the applied (direct) path over MCP requires a per-key grant.
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    resp = await _mcp_call(client, agent_tok, "create_document", {"title": "Applied", "content": "body", "parent_id": None})
    result = _result(resp)
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "applied"
    _assert_plain_id(payload["doc_id"])
