"""search_materials `corpus`: one switch matching the project's entity model.

Plan: search-materials-corpus. `include_docs`/`include_refs` encoded three kinds of
material in two booleans, and memory had no switch — so memory-only search was
simultaneously prescribed ("set both false", the old description) and forbidden (the
sources-read rule in the same description). The contract is now ONE parameter,
`corpus: "all" | "memory" | "documents" | "references"` (default `"all"`), moved in
ONE step across all three argument points: the advertised JSON schema (tool_defs),
the request model (ToolSearch), and the hand-written MCP parse (dispatch).

Kind gating per value (documents = entity docs, references = raw sources, memory =
distilled facts):
  all        → everything (today's behavior)
  memory     → memory only
  documents  → documents only — memory EXCLUDED: asking for the raw artifact kind is
               not a request to keep distilled facts (the old booleans kept memory
               unconditionally; `documents`/`references` narrow it away by name)
  references → references only (memory excluded, same reason)

`corpus` narrows the SEARCH, never the read: `read_document` works on any id under
any value, which keeps memory → `sources` → `read_document` usable from
`corpus: "memory"`.

The tests that asserted the old parameters moved here with the contract: the derived
forwarding guard (from test_search_mode_forwarding) and the chip args view (now at
the tool declaration — plugin presentation.test.ts, plan collapse-agent-stack step
6); the direct-layer call site test was updated in place
(test_search_exec_units — its subject is the parent-map scan, only its kwargs carry
the contract).
"""

import hashlib
import secrets
from types import SimpleNamespace

import pytest

# ─── helpers ──────────────────────────────────────────────────────────────────


def _search_spec() -> dict:
    from agent.tools import AGENT_TOOLS

    return next(t for t in AGENT_TOOLS if t["function"]["name"] == "search_materials")


def _search_schema_props() -> set[str]:
    """The advertised `parameters.properties` keys, derived from the single schema
    source (AGENT_TOOLS) — never hand-listed. The forwarding guard asserts every one
    of these except `intent` reaches the executor, so it fails the day someone
    advertises a new parameter and forgets the forwarder."""
    return set(_search_spec()["function"]["parameters"]["properties"].keys())


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
    return {"project_id": "p", "user": {"id": "u"}, "scope_root": None}


async def _make_agent_key(test_db, user_id: str, project_id: str) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    await create_record("api_keys", f"corpus-agent-key-{secrets.token_hex(4)}", {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": "",
        "token_hash": token_hash,
        "label": "agent",
        "capabilities": ["agent"],
    })
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


_KIND_ROWS = [
    {"id": "doc1", "is_memory": False, "is_reference": False},
    {"id": "ref1", "is_memory": False, "is_reference": True},
    {"id": "mem1", "is_memory": True, "is_reference": False},
]


# ─── 1. the advertised schema ─────────────────────────────────────────────────


def test_advertised_schema_carries_corpus_enum_and_no_old_switches():
    """One `corpus` enum replaces the two booleans in the advertised schema — the
    schema is served to BOTH surfaces (Pi loop via AGENT_TOOLS, MCP via
    schemas._agent_tool_to_mcp), so this is the contract's single advertised source."""
    props = _search_spec()["function"]["parameters"]["properties"]
    assert props["corpus"]["enum"] == ["all", "memory", "documents", "references"]
    assert props["corpus"]["default"] == "all"
    assert "include_docs" not in props, "the old boolean must leave the schema"
    assert "include_refs" not in props, "the old boolean must leave the schema"


def test_advertised_description_names_the_entity_model_not_the_booleans():
    """The description is rewritten from the tool's logic: never the dead
    both-false encoding (which contradicted the sources-read rule in the same
    description). The entity-model prose has ONE home — the corpus property
    description, beside the enum it explains: search_materials is the most
    expensive schema in the set, and a tool-level duplicate of the same
    sentences cost ~350 chars of per-turn context."""
    spec = _search_spec()
    desc = spec["function"]["description"]
    prop_desc = spec["function"]["parameters"]["properties"]["corpus"]["description"]
    assert "include_docs" not in desc and "include_refs" not in desc
    assert "memory" in prop_desc
    assert "narrows the search" in prop_desc.lower()
    assert "all" in prop_desc  # the default is advertised in prose too
    assert "narrows the search" not in desc.lower(), (
        "value prose must not be mirrored into the tool-level description"
    )


# ─── 2. kind gating per corpus value (direct layer) ───────────────────────────


@pytest.mark.parametrize("corpus,expected_ids", [
    ("all", {"doc1", "ref1", "mem1"}),
    ("memory", {"mem1"}),
    ("documents", {"doc1"}),
    ("references", {"ref1"}),
])
def test_filter_by_kind_returns_only_the_corpus_kinds(corpus, expected_ids):
    """Each corpus value returns only its kinds. The semantic change vs the old
    booleans: `documents`/`references` EXCLUDE memory (the old gate passed a memory
    fact regardless of the switches), and `memory` excludes both raw kinds —
    previously reachable only as the undocumented both-false residue."""
    from agent.search_exec import _filter_by_kind

    kept = _filter_by_kind(_KIND_ROWS, corpus=corpus)
    assert {r["id"] for r in kept} == expected_ids


# ─── 3. retrieval-layer marshalling per corpus value (semantic layer) ────────


@pytest.mark.asyncio
@pytest.mark.parametrize("corpus,inc_docs,inc_refs,inc_mem", [
    ("all", True, True, True),
    ("memory", False, False, True),
    ("documents", True, False, False),
    ("references", False, True, False),
])
async def test_retrieve_context_flags_per_corpus(
    corpus, inc_docs, inc_refs, inc_mem, monkeypatch,
):
    """`corpus` is marshalled to the retrieval-layer flags (which are themselves
    unchanged) at the single retrieve_context boundary: docs/refs off for `memory`,
    memory off for the single-raw-kind values. include_memory=False is what excludes
    distilled facts from `documents`/`references` — no post-filter layer to drift."""
    from unittest.mock import AsyncMock

    mock = AsyncMock(return_value=SimpleNamespace(error=None, hits=[]))
    monkeypatch.setattr("retrieval.retrieve_context", mock)

    from agent.search_exec import _retrieve_semantic_result

    await _retrieve_semantic_result(
        project_id="p", query="q", corpus=corpus, k=5, allowed=None,
    )
    kwargs = mock.call_args.kwargs
    assert kwargs["include_documents"] is inc_docs
    assert kwargs["include_references"] is inc_refs
    assert kwargs["include_memory"] is inc_mem


# ─── 4. forwarding: both surfaces (MOVED from test_search_mode_forwarding) ────


@pytest.mark.asyncio
async def test_http_corpus_reaches_executor(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    pid, _, admin_uid = project_with_doc
    mock = _patch_executor(monkeypatch)
    token = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/search_materials",
        json={"query": "x", "corpus": "references"}, headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    assert mock.call_args.kwargs.get("corpus") == "references"


@pytest.mark.asyncio
async def test_mcp_corpus_reaches_executor(monkeypatch):
    from mcp_gateway.dispatch import dispatch_tool

    mock = _patch_executor(monkeypatch)
    await dispatch_tool(
        "search_materials", {"query": "x", "corpus": "references"}, _mcp_ctx(),
    )
    assert mock.call_args.kwargs.get("corpus") == "references"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["http", "mcp"])
async def test_omitted_corpus_means_all(
    surface, client, test_db, admin_user, project_with_doc, monkeypatch,
):
    """No value = `all` = everything except nothing: memory included by
    construction, not by comment. The default must MARSHAL explicitly to "all" on
    both surfaces (a None corpus reaching _CORPUS_FLAGS would be a 500)."""
    mock = _patch_executor(monkeypatch)

    if surface == "http":
        pid, _, admin_uid = project_with_doc
        token = await _make_agent_key(test_db, admin_uid, pid)
        resp = await client.post(
            "/api/tool/search_materials",
            json={"query": "x"}, headers=_hdr(token),
        )
        assert resp.status_code == 200, resp.text
    else:
        from mcp_gateway.dispatch import dispatch_tool
        await dispatch_tool("search_materials", {"query": "x"}, _mcp_ctx())

    assert mock.call_args.kwargs.get("corpus") == "all"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["http", "mcp"])
async def test_every_advertised_property_reaches_executor(
    surface, client, test_db, admin_user, project_with_doc, monkeypatch,
):
    """The derived forwarding guard (moved here from test_search_mode_forwarding —
    its sentinels asserted the old booleans). For EVERY advertised property EXCEPT
    `intent`, the value sent must survive into the executor kwargs on BOTH surfaces:
    walking `properties` fails the day someone advertises a new parameter and
    forgets the forwarder — exactly how `mode` shipped dropped. `intent` is the one
    documented exemption: display-only (read off raw args by the tool's declared
    presentation — the plugin's presentation.ts)."""
    sent = {
        "query": "corpus-sentinel", "mode": "exact", "corpus": "memory", "k": 11,
        "under_document_id": "22222222-2222-2222-2222-222222222222",
        "intent": "human-readable intent",
    }
    mock = _patch_executor(monkeypatch)

    if surface == "http":
        pid, _, admin_uid = project_with_doc
        token = await _make_agent_key(test_db, admin_uid, pid)
        resp = await client.post(
            "/api/tool/search_materials", json=sent, headers=_hdr(token),
        )
        assert resp.status_code == 200, resp.text
    else:
        from mcp_gateway.dispatch import dispatch_tool
        await dispatch_tool("search_materials", dict(sent), _mcp_ctx())

    kwargs = mock.call_args.kwargs
    for prop in _search_schema_props() - {"intent"}:
        assert prop in kwargs, (
            f"{surface}: advertised property {prop!r} never reached the executor"
        )
        assert kwargs[prop] == sent[prop], (
            f"{surface}: {prop} forwarded as {kwargs[prop]!r}, expected {sent[prop]!r}"
        )
    assert "intent" not in kwargs


# ─── 5. validation policy ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_unknown_corpus_is_422(
    client, test_db, admin_user, project_with_doc,
):
    """The Literal on ToolSearch turns a misspelled corpus into an actionable 422
    Pi's own loop can self-correct from — same policy as `mode` on this surface."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/search_materials",
        json={"query": "x", "corpus": "everything"}, headers=_hdr(token),
    )
    assert resp.status_code == 422, resp.text
    assert "corpus" in resp.text


@pytest.mark.asyncio
async def test_mcp_unknown_corpus_errors_naming_valid_values(monkeypatch):
    """MCP REJECTS an unknown corpus instead of coercing — the deliberate OPPOSITE
    of `mode`'s coercion policy: `mode` was decorative until recently, but a
    silently unapplied `corpus` returns raw material to a caller who asked for
    distilled facts, and looks plausible. The error names `corpus` and every valid
    value so a third-party model can self-correct in one step."""
    from fastapi import HTTPException
    from mcp_gateway.dispatch import dispatch_tool

    with pytest.raises(HTTPException) as exc:
        await dispatch_tool(
            "search_materials", {"query": "x", "corpus": "everything"}, _mcp_ctx(),
        )
    detail = str(exc.value.detail)
    assert "corpus" in detail
    for value in ("all", "memory", "documents", "references"):
        assert value in detail, f"error must name the valid value {value!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_args", [
    {"include_docs": False},
    {"include_refs": False},
    {"include_docs": False, "include_refs": False},
])
async def test_mcp_old_switch_names_are_rejected(legacy_args, monkeypatch):
    """The old boolean names are an ERROR on the MCP surface, not a silently
    ignored no-op: pydantic's extra=ignore would drop them on the HTTP path and the
    search would run as `all` — plausible-looking and wrong. The error names
    `corpus` (and its values) so the caller updates in one step."""
    from fastapi import HTTPException
    from mcp_gateway.dispatch import dispatch_tool

    with pytest.raises(HTTPException) as exc:
        await dispatch_tool(
            "search_materials", {"query": "x", **legacy_args}, _mcp_ctx(),
        )
    assert "corpus" in str(exc.value.detail)


# ─── 6. the activity chip ──────────────────────────────────────────────────
# The chip's args-view whitelist (corpus / intent / mode / under_document_id)
# moved to the TOOL declaration (plugin presentation.ts, plan
# collapse-agent-stack step 6) — covered by test/presentation.test.ts.
