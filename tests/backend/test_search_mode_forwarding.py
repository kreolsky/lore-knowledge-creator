"""search_materials `mode` must reach the matcher on BOTH served surfaces.

Plan: search-mode-reaches-the-matcher. The advertised `mode` parameter was dropped by
the request model (ToolSearch) before it reached the executor on BOTH surfaces, so
`exact` was unreachable while the tool description promised it — and the activity layer
rendered `mode: exact` for a call that actually ran `semantic`. These tests pin the fix:
`mode` survives into the executor call on both surfaces, and the two surfaces differ in
their validation policy BY DESIGN (HTTP rejects an invalid mode as 422; MCP coerces it to
`semantic` so a hallucinated enum spelling from a third-party model does not spend the
caller's turn on a parameter that was decorative until this change).

A/B assert forwarding; D/E pin the divergent validation policy — they are the same
input (`mode: "banana"`) with opposite expectations, and each names the other so a later
reader cannot "fix" one into the other. The derived every-advertised-property guard
(Test C) moved to tests/backend/test_search_corpus.py with the `corpus` contract —
its sentinels asserted the old include switches.
"""

import hashlib
import secrets

import pytest

# ─── Test helpers ─────────────────────────────────────────────────────────────


async def _make_agent_key(test_db, user_id: str, project_id: str) -> str:
    """Insert a project-scoped agent API key and return the plaintext token."""
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    await create_record("api_keys", f"mode-agent-key-{secrets.token_hex(4)}", {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": "",  # project-scoped
        "token_hash": token_hash,
        "label": "agent",
        "capabilities": ["agent"],
    })
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _patch_executor(monkeypatch):
    """Replace the re-exported search_materials_tool with a spy that records kwargs.

    Both live surfaces import it lazily from agent.readonly_executors, so
    patching the attribute there reaches the HTTP handler (reads.py) AND the MCP
    dispatcher (dispatch.py) at call time."""
    from unittest.mock import AsyncMock

    mock = AsyncMock(return_value={"hits": []})
    monkeypatch.setattr(
        "agent.readonly_executors.search_materials_tool", mock,
    )
    return mock


def _mcp_ctx() -> dict:
    """A minimal dispatch ctx — only project_id / user / scope_root are read, and the
    executor is mocked, so no DB access happens on the dispatch-direct path."""
    return {"project_id": "p", "user": {"id": "u"}, "scope_root": None}


# ─── A: HTTP route forwards mode ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_search_mode_exact_reaches_executor(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    """Test A — POST /api/tool/search_materials with mode:"exact" reaches the executor
    as exact. Fails today: reads.py drops mode (ToolSearch has no field), so the
    executor always runs semantic."""
    pid, _, admin_uid = project_with_doc
    mock = _patch_executor(monkeypatch)
    token = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/search_materials",
        json={"query": "x", "mode": "exact"}, headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    assert mock.await_count == 1
    assert mock.call_args.kwargs.get("mode") == "exact"


# ─── B: MCP dispatch forwards mode ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_dispatch_mode_exact_reaches_executor(monkeypatch):
    """Test B — the MCP dispatch path forwards mode:"exact" to the executor. Fails
    today: dispatch.py passes no mode → always semantic. The twin of A on the MCP
    surface (raw args, no request model)."""
    from mcp_gateway.dispatch import dispatch_tool

    mock = _patch_executor(monkeypatch)

    await dispatch_tool("search_materials", {"query": "x", "mode": "exact"}, _mcp_ctx())

    assert mock.await_count == 1
    assert mock.call_args.kwargs.get("mode") == "exact"


# ─── D: HTTP rejects an invalid mode (422) ────────────────────────────────────


@pytest.mark.asyncio
async def test_http_invalid_mode_is_422(client, test_db, admin_user, project_with_doc):
    """Test D — mode:"banana" on the HTTP route is REFUSED (422), never silently
    coerced to semantic. The Literal on ToolSearch turns a misspelled mode into an
    actionable error Pi's own loop can self-correct from. The MCP path deliberately
    does the OPPOSITE (see test_mcp_invalid_mode_is_coerced) because a third-party model
    cannot act on it. Do not "fix" one into the other."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/search_materials",
        json={"query": "x", "mode": "banana"}, headers=_hdr(token),
    )
    assert resp.status_code == 422, resp.text


# ─── E: MCP coerces an invalid mode (no raise) ────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_invalid_mode_is_coerced(monkeypatch):
    """Test E — mode:"banana" through dispatch_tool is COERCED to semantic and the
    search still runs (no raise). Same input as test D, opposite expectation BY DESIGN:
    MCP's raw args arrive from a third-party model that hallucinates enum spellings
    (fuzzy/literal/keyword); turning them into a hard error spends the caller's turn on
    a parameter that was decorative until this change. Do not "fix" one into the other."""
    from mcp_gateway.dispatch import dispatch_tool

    mock = _patch_executor(monkeypatch)

    result = await dispatch_tool(
        "search_materials", {"query": "x", "mode": "banana"}, _mcp_ctx(),
    )

    # No raise; the coerced mode reached the executor as semantic.
    assert mock.await_count == 1
    assert mock.call_args.kwargs.get("mode") == "semantic"
    assert result == {"hits": []}
