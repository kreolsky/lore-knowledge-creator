"""Public /semantic-search route: subtree narrow + memory-only mode.

Plan: .kilo/plans/1786753800000-semantic-search-scope-and-memory-only.md.

The agent surface already expresses "consult only the distilled knowledge of a
subtree" (search_exec under_document_id + the provenance narrow in
retrieval.py). These tests pin the SAME contract on the public user-facing
route:

- absent under_document_id ⇒ whole project (today's behavior — the INVARIANT
  in retrieval/search_exec);
- under_document_id ⇒ docs/refs narrowed by subtree (SQL pushdown), memory by
  PROVENANCE and served WITH `sources`;
- include_docs=false & include_refs=false ⇒ memory-only: only memory hits,
  whole fact bodies (no fourth route switch — D2);
- unknown / cross-project / soft-deleted root ⇒ uniform 404 (no existence
  oracle — mirrors search_exec);
- a document-scoped Bearer key: scope_root is a CEILING — an out-of-scope root
  403s naming the remedy; an in-scope root narrows to the intersection.

The corpus is the narrow_corpus from test_retrieval_subtree_narrow (imported,
not duplicated): two subtrees, facts parented OUTSIDE them, provenance via
hosted references.
"""

import hashlib
import secrets

import pytest
from test_retrieval_subtree_narrow import QUERY_VEC, TOKEN


@pytest.fixture(autouse=True)
def _fake_query_embeddings(monkeypatch):
    """Pin the query embedding — same contract as test_retrieval_subtree_narrow."""
    import retrieval

    async def _embed(texts, **kwargs):
        return [list(QUERY_VEC) for _ in texts]

    monkeypatch.setattr(retrieval, "embed_texts", _embed)


async def _make_widget_key(test_db, user_id: str, project_id: str, scope_root: str = "") -> str:
    """Mint a Bearer key as the route's resolver accepts it (widget capability).

    `document_id` is the scope root ('' = whole project) — api_key_auth.
    """
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    await create_record("api_keys", f"search-route-key-{secrets.token_hex(4)}", {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": scope_root,
        "token_hash": token_hash,
        "label": "search-route",
        "capabilities": ["widget"],
    })
    return token


def _hits(data: dict) -> dict[str, dict]:
    return {h["parent_id"]: h for h in data["hits"]}


async def _get(client, pid, token, path):
    return await client.get(
        f"/api/projects/{pid}/semantic-search{path}",
        cookies={"lore_session": token},
    )


# ─── (a) No param ⇒ unchanged, memory present ────────────────────────────────


async def test_no_under_document_id_is_whole_project(client, admin_user, narrow_corpus):
    """Absent param ⇒ today's behavior: every fact served, out-subtree docs too."""
    _, token = admin_user
    pid = narrow_corpus["pid"]
    corpus = narrow_corpus

    resp = await _get(client, pid, token, f"?q={TOKEN}")
    assert resp.status_code == 200, resp.text
    hits = _hits(resp.json())

    for f in ("fact_in", "fact_out", "fact_merged", "fact_soft", "fact_empty"):
        assert corpus[f] in hits, f"{f} must be served with no narrow"
    assert corpus["host_out"] in hits
    assert corpus["ref_out"] in hits


# ─── (b) under_document_id ⇒ subtree + provenance-scoped memory ──────────────


async def test_under_document_id_narrows_and_keeps_memory_by_provenance(
    client, admin_user, narrow_corpus,
):
    _, token = admin_user
    pid = narrow_corpus["pid"]
    corpus = narrow_corpus

    resp = await _get(client, pid, token, f"?q={TOKEN}&under_document_id={corpus['root']}")
    assert resp.status_code == 200, resp.text
    hits = _hits(resp.json())

    # Docs/refs narrowed by tree position.
    assert corpus["host_in"] in hits
    assert corpus["ref_in"] in hits
    assert corpus["host_out"] not in hits
    assert corpus["ref_out"] not in hits

    # Memory by provenance: in-host sources kept (WITH sources), out/none dropped.
    hit = hits.get(corpus["fact_in"])
    assert hit is not None, "provenance-scoped fact dropped under the narrow"
    assert hit["kind"] == "memory"
    assert any(s["id"] == corpus["ref_in"] for s in hit.get("sources", [])), (
        "the memory hit arrived without its source references"
    )
    assert corpus["fact_merged"] in hits  # ∃-semantics: one inside source is enough
    assert corpus["fact_soft"] in hits  # soft-deleted source still resolves
    assert corpus["fact_out"] not in hits
    assert corpus["fact_empty"] not in hits


# ─── (c) include_docs=false & include_refs=false ⇒ memory-only ───────────────


async def test_memory_only_via_two_switches(client, admin_user, narrow_corpus):
    """D2: memory-only is the two existing switches — only memory hits arrive,
    each as the WHOLE fact body (a fact is served whole, never windowed)."""
    _, token = admin_user
    pid = narrow_corpus["pid"]
    corpus = narrow_corpus

    resp = await _get(client, pid, token, f"?q={TOKEN}&include_docs=false&include_refs=false")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["hits"], "memory-only returned nothing"
    assert all(h["kind"] == "memory" for h in data["hits"]), (
        f"non-memory kinds leaked: {[h['kind'] for h in data['hits']]}"
    )
    hits = _hits(data)
    for f in ("fact_in", "fact_out", "fact_merged", "fact_soft", "fact_empty"):
        assert corpus[f] in hits, f"{f} must be served memory-only (whole project)"
    # Whole fact bodies, not query-windowed fragments of them.
    assert hits[corpus["fact_in"]]["snippet"] == f"{TOKEN} fact from an inside source"
    assert hits[corpus["fact_out"]]["snippet"] == f"{TOKEN} fact from an outside source"


async def test_memory_only_under_narrow_still_provenance_scoped(
    client, admin_user, narrow_corpus,
):
    """The motivating workflow: memory-only + subtree = ONLY the facts the
    subtree's own material produced."""
    _, token = admin_user
    pid = narrow_corpus["pid"]
    corpus = narrow_corpus

    resp = await _get(
        client, pid, token,
        f"?q={TOKEN}&include_docs=false&include_refs=false&under_document_id={corpus['root']}",
    )
    assert resp.status_code == 200, resp.text
    hits = _hits(resp.json())

    assert set(hits) == {corpus["fact_in"], corpus["fact_merged"], corpus["fact_soft"]}, (
        f"memory-only narrow returned {sorted(hits)}"
    )


# ─── (d) Root validation ⇒ uniform 404 ───────────────────────────────────────


async def test_unknown_root_404(client, admin_user, narrow_corpus):
    _, token = admin_user
    pid = narrow_corpus["pid"]
    resp = await _get(client, pid, token, f"?q={TOKEN}&under_document_id=no-such-doc")
    assert resp.status_code == 404


async def test_cross_project_root_404(client, admin_user, narrow_corpus, test_db):
    """A real document from ANOTHER project is still a 404 (uniform, no oracle)."""
    _, token = admin_user
    pid = narrow_corpus["pid"]
    from db import create_record

    await create_record("documents", "foreign-root", {
        "project_id": "another-project", "parent_id": None,
        "title": "Foreign", "content": "", "path": "foreign-root",
    })
    resp = await _get(client, pid, token, "?q=vertigo&under_document_id=foreign-root")
    assert resp.status_code == 404


async def test_soft_deleted_root_404(client, admin_user, narrow_corpus, test_db):
    _, token = admin_user
    pid = narrow_corpus["pid"]
    corpus = narrow_corpus
    await test_db.query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": corpus["host_out"]},
    )
    resp = await _get(client, pid, token, f"?q={TOKEN}&under_document_id={corpus['host_out']}")
    assert resp.status_code == 404


# ─── (e)/(f) Scoped Bearer key: scope_root is a ceiling ─────────────────────


async def test_scoped_key_out_of_scope_root_403(client, narrow_corpus, test_db):
    """A key scoped to `root` + a narrow under an out-of-scope doc ⇒ 403 naming
    the remedy (get_project_structure), never silent empty hits."""
    corpus = narrow_corpus
    token = await _make_widget_key(test_db, corpus["uid"], corpus["pid"], corpus["root"])

    resp = await client.get(
        f"/api/projects/{corpus['pid']}/semantic-search"
        f"?q={TOKEN}&under_document_id={corpus['host_out']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "outside" in detail.lower()
    assert "get_project_structure" in detail


async def test_scoped_key_in_scope_root_intersects(client, narrow_corpus, test_db):
    """Key scope (root's subtree) ∩ requested (host_in's subtree) — the narrow
    may only narrow further, and memory follows provenance inside it."""
    corpus = narrow_corpus
    token = await _make_widget_key(test_db, corpus["uid"], corpus["pid"], corpus["root"])

    resp = await client.get(
        f"/api/projects/{corpus['pid']}/semantic-search"
        f"?q={TOKEN}&under_document_id={corpus['host_in']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    hits = _hits(resp.json())

    assert corpus["host_in"] in hits
    assert corpus["ref_in"] in hits
    # The key's root itself is OUTSIDE the requested intersection.
    assert corpus["root"] not in hits
    assert corpus["host_out"] not in hits
    assert corpus["ref_out"] not in hits
    # Provenance within the intersection: ref_in and ref_soft host under host_in.
    assert corpus["fact_in"] in hits
    assert corpus["fact_merged"] in hits
    assert corpus["fact_soft"] in hits
    assert corpus["fact_out"] not in hits
