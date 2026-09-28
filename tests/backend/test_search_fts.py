"""FTS index for project search.

The FULLTEXT indexes replace the string::contains SCAN; ranking stays match_count.
The first group covers the UI route (`/api/projects/{id}/search`): RU stemming,
3-char title prefix, the readiness guard (a silently-swallowed DDL failure would
leave no index), and the fallback (index absent → substring scan still returns
results).

The second group drives the AGENT two-layer search (`_search_materials_exec` in
backend/agent/search_exec.py) directly — the path the
search-materials-effective-fixes plan repairs: it missed inflected queries (F1,
CONTAINS-only predicate) and concatenated its layers instead of fusing them (F4).
"""

import pytest


@pytest.fixture
async def fts_project(client, admin_user):
    uid, token = admin_user
    cookies = {"lore_session": token}
    resp = await client.post("/api/projects", json={"name": "FTS Test"}, cookies=cookies)
    pid = resp.json()["project_id"]

    # RU morphology: body carries inflected forms of мир (мира/миру).
    await client.post("/api/documents", json={
        "project_id": pid, "title": "Valdris Geography",
        "content": "История мира и судьба миру принадлежит героям.",
    }, cookies=cookies)
    return pid, uid, token


async def test_ru_stemming_finds_inflected_forms(client, fts_project):
    """`мир` must find a body containing `мира`/`миру` (snowball russian)."""
    pid, _, token = fts_project
    resp = await client.get(f"/api/projects/{pid}/search?q=мир", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert any("Valdris" in r["title"] for r in results), results


async def test_title_prefix_3char_edgengram(client, fts_project):
    """`val` (3-char prefix) must find `Valdris` via the title edgengram(3,10)."""
    pid, _, token = fts_project
    resp = await client.get(f"/api/projects/{pid}/search?q=val", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert any("Valdris" in r["title"] for r in results), results


async def test_fts_indexes_report_ready(client, admin_user):
    """Both FULLTEXT indexes must exist and be `ready` — guards against a
    FULLTEXT/syntax regression being silently swallowed by apply_schema."""
    from db import get_db
    db = await get_db()
    for name in ("idx_documents_fts_title", "idx_documents_fts_content"):
        info = await db.query(f"INFO FOR INDEX {name} ON documents")
        if isinstance(info, list):
            info = info[0]
        assert info["building"]["status"] == "ready", (name, info)


async def test_falls_back_to_substring_when_fts_unavailable(client, fts_project, monkeypatch):
    """When the FTS query raises (index missing/not ready), the substring scan runs
    and results are still returned — no 500, no empty degradation."""
    pid, _, token = fts_project
    from db import get_db
    db = await get_db()
    orig_query = db.query

    async def flaky(sql, *args, **kwargs):
        if "@0@" in sql:
            raise RuntimeError("simulated: FTS index absent")
        return await orig_query(sql, *args, **kwargs)

    monkeypatch.setattr(db, "query", flaky)
    # substring match on a literal form present in the body
    resp = await client.get(f"/api/projects/{pid}/search?q=История", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    assert any("Valdris" in r["title"] for r in resp.json()["results"])


# ─── Agent search path (_search_materials_exec) ──────────────────────────────
# Drives the agent two-layer search directly, not the UI route above. These encode
# the search-materials-effective-fixes plan: S1 (FTS OR CONTAINS union), S2 (RRF
# fusion), S3 (intent + mode). They fail BEFORE the implementation lands.


def _user(uid: str) -> dict:
    return {"user_id": uid}


async def _seed_doc(test_db, *, doc_id: str, project_id: str, title: str,
                    content: str = "", is_memory: bool = False,
                    mem_active: bool | None = None, path: str | None = None) -> str:
    """Create a documents record directly — REST /api/documents exposes no is_memory."""
    from db import create_record

    await test_db.query("DELETE type::record('documents', $id)", {"id": doc_id})
    payload = {
        "project_id": project_id, "parent_id": None, "title": title,
        "content": content, "path": path or f"{doc_id}.md",
        "is_index": False, "is_reference": False,
    }
    if is_memory:
        payload["is_memory"] = True
        payload["mem_active"] = bool(mem_active) if mem_active is not None else True
    await create_record("documents", doc_id, payload)
    return doc_id


# S1 — the lexical layer gets morphology =====================================


async def test_agent_path_finds_inflected_query(fts_project):
    """S1: the agent lexical layer finds a doc via STEMMING when the query is an
    inflected form whose string is NOT a substring of the body (the F1 case:
    `гепарином`-shaped queries returned 1 of 8 docs).

    `миром` (instrumental) is absent from the body literally (`мира`/`миру` are),
    but snowball(russian) reduces all three to one stem. The old CONTAINS-only
    predicate misses this; the S1 union (FTS OR CONTAINS) finds it.
    """
    pid, uid, _token = fts_project
    from agent.readonly_executors import _search_materials_exec

    result = await _search_materials_exec(
        project_id=pid, user=_user(uid), query="миром", k=5,
    )
    titles = {h["title"] for h in result["hits"]}
    assert any("Valdris" in t for t in titles), result


async def test_agent_path_keeps_substring_inside_word(client, test_db, fts_project):
    """S1 guard (D2a, direction A): a doc reachable ONLY by the substring scan —
    `сосуд` inside `сосудистые` — stays findable after the FTS disjunct is added.
    The stemmer does NOT match here (сосуд ≠ сосудист), so dropping CONTAINS would
    lose the hit; the union keeps it. This is the test that breaks if the predicate
    is ever "simplified" back to FTS alone.
    """
    pid, _uid, _token = fts_project
    await _seed_doc(
        test_db, doc_id="vessel-prefix", project_id=pid,
        title="Vessel Notes", content="Раздел про сосудистые растения.",
    )
    from agent.readonly_executors import _search_materials_exec

    result = await _search_materials_exec(
        project_id=pid, user=_user(_uid), query="сосуд", k=5,
    )
    titles = {h["title"] for h in result["hits"]}
    assert any("Vessel" in t for t in titles), result


# S2 — fuse the layers by rank (RRF) =========================================


async def test_fusion_brings_both_layer_doc_to_head():
    """S2: a doc found by BOTH layers (direct rank-2 + semantic rank-1) outranks a
    doc found by direct only (rank-1). The old 'direct-first, then unseen semantic'
    concatenation could not do this — it left every semantic signal behind every
    direct hit. Also asserts the both-layer doc is labelled `source: "both"`.

    Mocked layers: direct=[d1(rank1), c1(rank2)], semantic=[c1(rank1)].
    Concat: [d1, c1]. RRF: c1 = 1/62 + 1/61 > d1 = 1/61 → [c1, d1].
    """
    from unittest.mock import AsyncMock, patch

    from agent.readonly_executors import _search_materials_exec

    from retrieval import RetrievalHit, RetrievalResult

    async def fake_query(stmt, params=None):
        s = stmt.lower()
        if "limit 200" in s:
            return [
                {"id": "d1", "title": "fusion direct", "content": "",
                 "is_reference": False, "parent_id": None, "is_memory": False,
                 "mem_active": False, "updated_at": None, "sort_key": "a"},
                {"id": "c1", "title": "beta", "content": "",
                 "is_reference": False, "parent_id": None, "is_memory": False,
                 "mem_active": False, "updated_at": None, "sort_key": "b"},
            ]
        if "parent_id from documents" in s:
            return [{"id": "d1", "parent_id": None}, {"id": "c1", "parent_id": None}]
        return []

    sem = RetrievalResult(hits=[RetrievalHit(
        kind="document", parent_id="c1", parent_title="beta", heading=None,
        snippet="sem", offset_start=None, offset_end=None, score=0.9, sources=[],
    )])
    with patch("retrieval.retrieve_context", new=AsyncMock(return_value=sem)), \
         patch("agent.search_exec.get_db") as gdb, \
         patch("agent.search_exec.get_document_access",
               new=AsyncMock(return_value=True)):
        gdb.return_value.query = fake_query
        result = await _search_materials_exec(
            project_id="p1", user={"user_id": "u1"}, query="fusion", k=5,
        )
    assert result["hits"][0]["doc_id"] == "c1", result
    assert result["hits"][0]["source"] == "both", result


async def test_fusion_preserves_memory_kind_without_unverified_axis():
    """S2 guard: a memory fact found by both layers keeps `kind: "memory"` through
    RRF fusion — fusion must not relabel it as a plain document (the direct layer's
    memory labelling is authoritative). `source: "both"` is the S2-owned assertion.

    The `unverified` axis is RETIRED (plan phase-b-debt-payoff DEC1): the marker
    fired only on retired facts while claiming to mean "hand-edited" — a meaning it
    never measured — and flagging facts as unverified is not wanted. The assertion
    pins its absence from the fused payload so it cannot ride back silently.
    """
    from unittest.mock import AsyncMock, patch

    from agent.readonly_executors import _search_materials_exec

    from retrieval import RetrievalHit, RetrievalResult

    async def fake_query(stmt, params=None):
        s = stmt.lower()
        if "limit 200" in s:
            return [{"id": "m1", "title": "fusion fact", "content": "memory body",
                      "is_reference": False, "parent_id": None, "is_memory": True,
                      "updated_at": None, "sort_key": ""}]
        if "parent_id from documents" in s:
            return [{"id": "m1", "parent_id": None}]
        return []

    sem = RetrievalResult(hits=[RetrievalHit(
        kind="memory", parent_id="m1", parent_title="fusion fact", heading=None,
        snippet="memory body", offset_start=None, offset_end=None, score=0.8,
        sources=[{"id": "r1", "title": "src"}],
    )])
    with patch("retrieval.retrieve_context", new=AsyncMock(return_value=sem)), \
         patch("agent.search_exec.get_db") as gdb, \
         patch("agent.search_exec.get_document_access",
               new=AsyncMock(return_value=True)):
        gdb.return_value.query = fake_query
        result = await _search_materials_exec(
            project_id="p1", user={"user_id": "u1"}, query="fusion", k=5,
        )
    hits = {h["doc_id"]: h for h in result["hits"]}
    assert "m1" in hits, result
    assert hits["m1"]["kind"] == "memory", result
    assert "unverified" not in hits["m1"], result
    assert hits["m1"]["source"] == "both", result  # fails until S2


# S3 — intent + mode =========================================================


async def test_exact_mode_matches_literal_string_only(client, test_db, fts_project):
    """S3 (D9a, matcher-level): `exact` uses the CONTAINS scan ALONE — every exact hit
    literally contains the query string, so exact is a matcher-level subset of semantic
    (CONTAINS ⊆ CONTAINS∪FTS). This is the test that fails if a matcher (e.g. FTS) is
    ever added to exact alone: a stem-only hit would appear whose body does NOT contain
    the literal query.

    Why matcher-level and not result-set equality: top-k RESULT sets can differ between
    the modes when literal matches outnumber k (a slicing artifact — semantic's RRF can
    push a literal match out of its top-k while exact's rank_rows surfaces it). The
    D9a contract is recall-level ("an empty exact means the string is absent"), so the
    robust assertion is that exact never produces a hit the literal scan would not —
    i.e. every survivor contains the string verbatim.
    """
    pid, uid, _token = fts_project
    await _seed_doc(
        test_db, doc_id="vessel-exact", project_id=pid,
        title="Vessel Notes", content="Раздел про сосудистые растения.",
    )
    from agent.readonly_executors import _search_materials_exec

    from db import get_db

    db = await get_db()
    for q in ["сосуд", "миром", "val", "Valdris"]:
        ex = await _search_materials_exec(
            project_id=pid, user=_user(uid), query=q, k=10, mode="exact",
        )
        for h in ex["hits"]:
            rows = await db.query(
                "SELECT title, content FROM documents WHERE meta::id(id) = $id",
                {"id": h["doc_id"]},
            )
            row = rows[0] if isinstance(rows, list) and rows else {}
            blob = f"{row.get('title') or ''} {row.get('content') or ''}".lower()
            assert q.lower() in blob, (
                q, h["doc_id"], "exact hit must literally contain the query string"
            )


async def test_exact_mode_zero_hit_returns_warning(fts_project):
    """S3.2: an `exact` search that finds nothing must NOT return a bare empty hits
    array (which the model reads as 'nothing like this exists'). It appends to the
    existing `warnings` channel. An empty array without the warning is the
    regression. Fails until S3."""
    pid, uid, _token = fts_project
    from agent.readonly_executors import _search_materials_exec

    result = await _search_materials_exec(
        project_id=pid, user=_user(uid), query="zzznomatchxyz", k=5, mode="exact",
    )
    assert result["hits"] == [], result
    assert result.get("warnings"), result


async def test_intent_is_never_used_as_search_term(fts_project):
    """S3.1 contract: `intent` is display-only — it never reaches a matcher. The
    param is now ABSENT from the search surface entirely (the strongest form of
    the contract): supplying it is a TypeError, so no matcher can ever read it.
    The presentation layer reads intent off the RAW tool args (reads.py drops
    it before the executor; the args-view whitelist itself lives at the tool
    declaration now — plugin presentation.test.ts)."""
    import inspect

    from agent.readonly_executors import _search_materials_exec

    assert "intent" not in inspect.signature(_search_materials_exec).parameters, (
        "intent must not be a search parameter — display-only, read off raw args"
    )
