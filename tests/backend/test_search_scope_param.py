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


import pytest
from fastapi import HTTPException

# ─── Test helpers ─────────────────────────────────────────────────────────────


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


# ─── 5: the key's scope is the ceiling — intersection, never replacement ─────


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
