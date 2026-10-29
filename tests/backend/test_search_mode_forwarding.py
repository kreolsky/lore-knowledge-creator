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


import pytest

# ─── Test helpers ─────────────────────────────────────────────────────────────


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
