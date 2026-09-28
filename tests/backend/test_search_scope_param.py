"""search_materials `under_document_id` — the model-facing subtree narrow.

Plan: search-scope-param-and-dead-anchor (task B). Until now the only scope input
was the agent key's `scope_root` — a permission boundary the model can neither
set nor see — so "search only under this document" was not expressible. The new
OPTIONAL `under_document_id` narrows BOTH layers to that subtree; absent means
the WHOLE project (memory facts included). The key's scope stays the ceiling:
`allowed` is the INTERSECTION key_subtree ∩ requested_subtree, never a
replacement. An out-of-scope or unknown id is an ERROR naming the remedy, never
an empty hits array (empty hits reads to a model as "the project holds nothing").
Per-surface validation follows the `mode` precedent: HTTP hard-errors a malformed
value (422) and the executor 404/403s unknown/out-of-scope ids; MCP dispatch
coerces missing/empty to whole-project but still errors on a real out-of-scope
id (a permission answer, not a spelling one).
"""

import hashlib
import secrets

import pytest
from fastapi import HTTPException

# ─── Test helpers ─────────────────────────────────────────────────────────────


async def _make_agent_key(test_db, user_id: str, project_id: str,
                          document_id: str = "") -> str:
    """Insert an agent API key (project-scoped by default, subtree-scoped when
    `document_id` is set) and return the plaintext token."""
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    await create_record("api_keys", f"scope-agent-key-{secrets.token_hex(4)}", {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": document_id,  # '' = project-scoped
        "token_hash": token_hash,
        "label": "agent",
        "capabilities": ["agent"],
    })
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _search_prop(name: str) -> dict:
    """The advertised `parameters.properties` entry for search_materials, derived
    from the single schema source (AGENT_TOOLS) — never hand-written."""
    from agent.tools import AGENT_TOOLS

    spec = next(t for t in AGENT_TOOLS if t["function"]["name"] == "search_materials")
    return spec["function"]["parameters"]["properties"][name]


def _patch_executor(monkeypatch):
    """Replace the re-exported search_materials_tool with a spy that records kwargs
    (both live surfaces import it lazily from readonly_executors at call time)."""
    from unittest.mock import AsyncMock

    mock = AsyncMock(return_value={"hits": []})
    monkeypatch.setattr(
        "agent.readonly_executors.search_materials_tool", mock,
    )
    return mock


def _mcp_ctx(scope_root: str | None = None) -> dict:
    return {
        "project_id": "p", "user": {"id": "u"}, "scope_root": scope_root,
    }


def _fake_db(rows_200: list[dict], captured: list | None = None):
    """A get_db() stub whose candidate query returns `rows_200` and whose params
    are recorded into `captured` (for asserting the `allowed` filter set). When
    the statement carries the `allowed` param, rows are filtered by it — the
    fake must emulate the real `id IN $allowed` WHERE clause, not bypass it."""
    class _DB:
        async def query(self, stmt, params=None):
            params = dict(params or {})
            if captured is not None:
                captured.append((stmt, params))
            s = stmt.lower()
            if "limit 200" in s:
                if "allowed" in params:
                    rows = [r for r in rows_200 if r["id"] in set(params["allowed"])]
                else:
                    rows = rows_200
                return rows
            if "parent_id from documents" in s:
                return [{"id": r["id"], "parent_id": r.get("parent_id")} for r in rows_200]
            return []
    return _DB()


def _row(doc_id: str, *, title="t", content="", is_memory=False, parent_id=None):
    return {
        "id": doc_id, "title": title, "content": content, "is_reference": False,
        "parent_id": parent_id, "is_memory": is_memory,
        "mem_active": True, "updated_at": None, "sort_key": "",
    }


async def _run_exec(monkeypatch, rows_200, *, under, scope_root=None,
                    subtree_map=None, in_subtree_ret=True, root_row="valid"):
    """Drive _search_materials_exec with a faked DB + scope, returning (result,
    captured_params). `subtree_map` maps a root id → its subtree ids list;
    `root_row="valid"` seeds an existing same-project root, None = unknown id,
    a dict = that exact row."""
    from unittest.mock import AsyncMock, patch

    from agent.readonly_executors import _search_materials_exec

    from retrieval import RetrievalResult

    captured: list = []
    # scope.subtree_doc_ids / in_subtree / fetch_one are imported at call time
    # inside the executor — patch the scope/db module attributes they resolve from.
    async def _subtree_stub(root, _pid):
        if subtree_map is None:
            return [root]
        return subtree_map.get(root, [root])

    async def _in_subtree_stub(_root, _doc):
        return in_subtree_ret

    async def _fetch_one(_table, doc_id):
        if root_row == "valid":
            return {"id": doc_id, "project_id": "p1", "deleted_at": None}
        return root_row

    with patch("retrieval.retrieve_context",
               new=AsyncMock(return_value=RetrievalResult(hits=[]))), \
         patch("agent.search_exec.get_db",
               return_value=_fake_db(rows_200, captured)), \
         patch("agent.search_exec.get_document_access",
               new=AsyncMock(return_value=True)), \
         patch("agent.search_exec.fetch_one", new=_fetch_one), \
         patch("scope.subtree_doc_ids", new=_subtree_stub), \
         patch("scope.in_subtree", new=_in_subtree_stub):
        result = await _search_materials_exec(
            project_id="p1", user={"user_id": "u1"}, query="squery", k=5,
            under_document_id=under, scope_root=scope_root,
        )
    return result, captured


# ─── 1: the served schema advertises the narrow ──────────────────────────────


def test_schema_advertises_under_document_id():
    """The served search_materials schema carries `under_document_id`, and its
    description states the default (whole project) — the fact that makes the
    narrow opt-in rather than something a model assumes from a working
    document."""
    prop = _search_prop("under_document_id")
    assert prop.get("type") == "string"
    desc = prop.get("description", "")
    assert "whole project" in desc, desc


# ─── 2: forwarding on BOTH surfaces ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_forwards_under_document_id(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    pid, _, admin_uid = project_with_doc
    mock = _patch_executor(monkeypatch)
    token = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/search_materials",
        json={"query": "x", "under_document_id": "11111111-1111-1111-1111-111111111111"},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    assert mock.await_count == 1
    assert mock.call_args.kwargs.get("under_document_id") == (
        "11111111-1111-1111-1111-111111111111"
    )


@pytest.mark.asyncio
async def test_mcp_forwards_under_document_id(monkeypatch):
    from mcp_gateway.dispatch import dispatch_tool

    mock = _patch_executor(monkeypatch)
    await dispatch_tool(
        "search_materials",
        {"query": "x", "under_document_id": "11111111-1111-1111-1111-111111111111"},
        _mcp_ctx(),
    )
    assert mock.await_count == 1
    assert mock.call_args.kwargs.get("under_document_id") == (
        "11111111-1111-1111-1111-111111111111"
    )


@pytest.mark.asyncio
async def test_mcp_empty_under_document_id_is_whole_project(monkeypatch):
    """MCP coercion policy (the `mode` precedent): a missing/empty value is NOT
    an error on the MCP surface — it coerces to whole project (executor receives
    None), because a third-party model cannot act on a hard error for a spelling
    miss. A REAL out-of-scope id still errors (see the executor tests)."""
    from mcp_gateway.dispatch import dispatch_tool

    mock = _patch_executor(monkeypatch)
    await dispatch_tool(
        "search_materials", {"query": "x", "under_document_id": ""}, _mcp_ctx(),
    )
    assert mock.await_count == 1
    assert mock.call_args.kwargs.get("under_document_id") is None


@pytest.mark.asyncio
async def test_http_rejects_non_string_under_document_id(
    client, test_db, admin_user, project_with_doc,
):
    """HTTP validation policy (the `mode` precedent): a malformed value is a 422
    Pi's own loop can self-correct from — NOT a silent coercion."""
    pid, _, admin_uid = project_with_doc
    token = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post(
        "/api/tool/search_materials",
        json={"query": "x", "under_document_id": 123}, headers=_hdr(token),
    )
    assert resp.status_code == 422, resp.text


# ─── 3: absent ⇒ whole project (no filter, memory still present) ─────────────


@pytest.mark.asyncio
async def test_absent_under_document_id_is_whole_project(monkeypatch):
    """Absent ⇒ NO subtree filter runs (no `allowed` param reaches any query) and
    a memory fact is still served — the whole-project default is what keeps
    project memory visible (see the INVARIANT in search_exec)."""
    rows = [_row("mem1", title="fact", content="squery body", is_memory=True)]
    result, captured = await _run_exec(monkeypatch, rows, under=None)

    assert [h["doc_id"] for h in result["hits"]] == ["mem1"], result
    for stmt, params in captured:
        assert "IN $allowed" not in stmt, stmt
        assert "allowed" not in params, params


# ─── 4: set ⇒ BOTH layers narrowed ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_under_document_id_narrows_direct_layer(monkeypatch):
    """Set ⇒ the direct candidate pool is scoped to the requested subtree
    (`allowed` reaches the candidate query) and a memory fact outside the
    subtree drops FROM THE DIRECT LAYER (its `IN $allowed` filters by tree
    position, and fact-docs live outside every user subtree). Under a narrow,
    memory reaches the model via the SEMANTIC layer's provenance filter instead
    — see test_retrieval_subtree_narrow.py; this test pins the direct half
    only."""
    rows = [
        _row("u1", title="inside", content="squery", parent_id="under"),
        _row("mem1", title="memory fact", content="squery", is_memory=True),
    ]
    result, captured = await _run_exec(
        monkeypatch, rows, under="under",
        subtree_map={"under": ["under", "u1"]},
    )

    assert [h["doc_id"] for h in result["hits"]] == ["u1"], result
    cand = [(s, p) for s, p in captured if "limit 200" in s.lower()]
    assert cand, captured
    assert set(cand[0][1]["allowed"]) == {"under", "u1"}, cand[0][1]


@pytest.mark.asyncio
async def test_under_document_id_narrows_semantic_layer(monkeypatch):
    """Set ⇒ semantic hits outside the subtree are dropped by the post-filter
    (retrieve_context searches project-wide embeddings; `allowed` is the wall)."""
    from unittest.mock import AsyncMock, patch

    from retrieval import RetrievalHit, RetrievalResult

    sem = RetrievalResult(hits=[
        RetrievalHit(
            kind="document", parent_id="outside", parent_title="out",
            heading=None, snippet="s", offset_start=None, offset_end=None,
            score=0.9, sources=[],
        ),
        RetrievalHit(
            kind="document", parent_id="u1", parent_title="in",
            heading=None, snippet="s", offset_start=None, offset_end=None,
            score=0.8, sources=[],
        ),
    ])
    from agent.readonly_executors import _search_materials_exec

    async def _subtree(root, _pid):
        return ["under", "u1"]

    async def _fetch_one(_table, doc_id):
        return {"id": doc_id, "project_id": "p1", "deleted_at": None}

    with patch("retrieval.retrieve_context", new=AsyncMock(return_value=sem)), \
         patch("agent.search_exec.get_db",
               return_value=_fake_db([])), \
         patch("agent.search_exec.get_document_access",
               new=AsyncMock(return_value=True)), \
         patch("agent.search_exec.fetch_one", new=_fetch_one), \
         patch("scope.subtree_doc_ids", new=_subtree):
        result = await _search_materials_exec(
            project_id="p1", user={"user_id": "u1"}, query="squery", k=5,
            under_document_id="under",
        )
    assert [h["doc_id"] for h in result["hits"]] == ["u1"], result


# ─── 5: the key's scope is the ceiling — intersection, never replacement ─────


@pytest.mark.asyncio
async def test_scoped_key_intersects_requested_subtree(monkeypatch):
    """allowed = key_subtree ∩ requested_subtree. A scoped key whose model narrows
    FURTHER gets the intersection (the key's sibling `other` drops); a narrow can
    never WIDEN the key's reach."""
    rows = [_row("u1", title="inside", content="squery", parent_id="under")]
    result, captured = await _run_exec(
        monkeypatch, rows, under="under", scope_root="K",
        subtree_map={
            # the key's real subtree includes u1 (a descendant of under via K)
            "K": ["K", "under", "other", "u1"],
            "under": ["under", "u1"],
        },
    )

    cand = [(s, p) for s, p in captured if "limit 200" in s.lower()]
    assert cand, captured
    assert set(cand[0][1]["allowed"]) == {"under", "u1"}, cand[0][1]


@pytest.mark.asyncio
async def test_request_outside_key_subtree_is_error_not_empty(monkeypatch):
    """A request naming a document OUTSIDE the key's subtree is a 403 carrying
    scope.out_of_scope_detail (names the remedy) — NEVER an empty hits array,
    which a model reads as 'the project holds nothing about this'."""
    with pytest.raises(HTTPException) as ei:
        await _run_exec(
            monkeypatch, [], under="U", scope_root="K",
            subtree_map={"K": ["K", "k1"]}, in_subtree_ret=False,
        )
    assert ei.value.status_code == 403
    assert "outside this agent key's subtree scope" in ei.value.detail


@pytest.mark.parametrize("root_row", [
    None,  # unknown id
    {"id": "U", "project_id": "OTHER", "deleted_at": None},  # cross-project
    {"id": "U", "project_id": "p1", "deleted_at": "2026-01-01"},  # deleted
])
@pytest.mark.asyncio
async def test_unknown_or_cross_project_root_is_404(monkeypatch, root_row):
    """An unknown / cross-project / deleted `under_document_id` is a 404 (uniform,
    no existence oracle) — an error, never empty hits."""
    with pytest.raises(HTTPException) as ei:
        await _run_exec(
            monkeypatch, [], under="U", root_row=root_row,
        )
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_empty_intersection_is_error_not_whole_project(monkeypatch):
    """A legitimately EMPTY intersection must be an error — it must never be
    coerced into the `allowed = None` 'do not filter' sentinel (`subtree_doc_ids`
    returns [] for unscoped; a falsy-set bug here would turn an impossible narrow
    into a whole-project search)."""
    with pytest.raises(HTTPException) as ei:
        await _run_exec(
            monkeypatch, [], under="U", scope_root="K",
            subtree_map={"K": ["K", "k1"], "U": ["U", "u1"]},
            in_subtree_ret=True,
        )
    assert ei.value.status_code == 403
    assert "in-scope" in ei.value.detail, ei.value.detail


# ─── 6: the activity layer renders the narrow ────────────────────────────────
# The under_document_id whitelist moved to the TOOL declaration (plugin
# presentation.ts, plan collapse-agent-stack step 6) — covered by
# test/presentation.test.ts.
