"""MCP-path weak-model tolerance tests — the accept side, driven end-to-end.

The advertised tool schemas (schemas._agent_tool_to_mcp) HIDE the legacy
spellings; dispatch._normalize_args ACCEPTS them. The schema projections and
the readonly/mutating suites pin pieces of this, but no test drove the legacy
spellings through the FULL /mcp path (ASGI gate → gateway input validation →
_normalize_args → executor) — the exact path a weak/legacy model takes.

Also pins TOLERANCE_HITS: dispatch's per-spelling hit counters, the measurable
exit criterion for the weak-model-tolerance DEBT (the layer can be deleted when
the counters read zero over a representative deployment window). Each test
resets the counters, drives ONE call, and asserts the exact per-spelling delta —
so a canonical call reading non-zero, or a tolerated call not counting, both
fail loudly.
"""

import hashlib
import json
import secrets

import pytest

# ─── Helpers (shared shape with the other MCP suites) ─────────────────────────


async def _make_agent_key(test_db, user_id: str, project_id: str, *,
                          auto_apply: bool = True) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"mcp-tol-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id,
        "document_id": "", "token_hash": token_hash, "label": "agent",
        "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


def _mcp_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


async def _make_doc(client, token: str, project_id: str, title: str,
                    content: str = "") -> str:
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
    return json.loads(resp_json["result"]["content"][0]["text"])


def _is_error(resp_json: dict) -> bool:
    return resp_json.get("result", {}).get("isError", False)


async def _mcp_call(client, token: str, name: str,
                    arguments: dict | None = None) -> dict:
    resp = await client.post(
        "/mcp", json=_rpc("tools/call", {"name": name, "arguments": arguments or {}}),
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _reset_counters() -> dict:
    """Clear dispatch's tolerance counters and return the live dict.

    The production code mutates this dict IN PLACE (never rebinds), so the
    reference held here observes the increments from subsequent calls."""
    from mcp_gateway.dispatch import TOLERANCE_HITS

    TOLERANCE_HITS.clear()
    return TOLERANCE_HITS


# ─── read_document: legacy `name` alias for document_id ───────────────────────


@pytest.mark.asyncio
async def test_read_document_name_alias_dispatches_and_counts(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A call whose canonical document_id is present-but-EMPTY while the legacy
    `name` spelling carries the real id: input validation passes (the required key
    exists), and _normalize_args folds the alias into document_id — the alias's
    MCP-path-reachable shape (a name-only call is 400'd by `required` at the SDK
    layer before dispatch, so the empty-canonical + alias pair is how a weak
    model's legacy spelling actually arrives). The spelling's counter increments
    exactly once."""
    pid, doc_id, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)
    hits = _reset_counters()

    data = await _mcp_call(client, agent_tok, "read_document",
                           {"document_id": "", "name": doc_id})
    assert not _is_error(data), data
    assert _result_text(data)["doc_id"] == doc_id
    assert hits.get("read_document.name_alias") == 1


@pytest.mark.asyncio
async def test_read_document_canonical_call_does_not_count(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A canonical document_id call must NOT touch the tolerance counter — the
    exit criterion only means something if canonical traffic reads zero."""
    pid, doc_id, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)
    hits = _reset_counters()

    data = await _mcp_call(client, agent_tok, "read_document",
                           {"document_id": doc_id})
    assert not _is_error(data), data
    assert "read_document.name_alias" not in hits


# ─── edit_table_cell: legacy flat single-cell (no edits[]) ────────────────────


@pytest.mark.asyncio
async def test_edit_table_cell_flat_legacy_shape_dispatches_and_counts(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A flat {document_id, table_id, row, column, old_value, new_value} call —
    the pre-batch spelling, no edits[] — is coalesced into a one-element batch
    and APPLIES; the spelling's counter increments exactly once. The table is
    created first via the advertised batch path (also the end-to-end exercise
    of the create_table route through the same gateway)."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "Tolerance Table Doc")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    made = await _mcp_call(client, agent_tok, "create_table", {
        "document_id": doc_id,
        "rows": [["Name", "HP"], ["Goblin", "3"]],
    })
    assert not _is_error(made), made
    table_id = _result_text(made)["table_id"]

    hits = _reset_counters()
    data = await _mcp_call(client, agent_tok, "edit_table_cell", {
        "document_id": doc_id, "table_id": table_id, "row": 1,
        "column": "HP", "old_value": "3", "new_value": "7",
    })
    assert not _is_error(data), data
    assert _result_text(data)["status"] == "applied"
    assert hits.get("edit_table_cell.flat_single_cell") == 1

    read = await _mcp_call(client, agent_tok, "read_document", {
        "document_id": doc_id, "tables": "inline",
    })
    tables = _result_text(read)["tables"]
    grid = next(t for t in tables if t["table_id"] == table_id)
    row1 = next(r for r in grid["rows"] if r["row"] == 1)
    assert "7" in row1["cells"], f"flat edit did not land: {row1}"


# ─── edit_document: legacy singular old_string/new_string ─────────────────────


@pytest.mark.asyncio
async def test_edit_document_singular_coalesce_counts(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """The singular old_string/new_string form coalesces into edits[] (the
    behavior itself is pinned by the mutating suite); this pins the third
    tolerance member's COUNTER so all three spellings of the DEBT class are
    measurable."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid,
                             "Tolerance Singular Doc", "Ancient tome, page one.")
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    hits = _reset_counters()
    data = await _mcp_call(client, agent_tok, "edit_document", {
        "document_id": doc_id, "old_string": "page one", "new_string": "page uno",
    })
    assert not _is_error(data), data
    assert _result_text(data)["status"] == "applied"
    assert hits.get("edit_document.legacy_singular") == 1
