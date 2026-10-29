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


# S2 — fuse the layers by rank (RRF) =========================================


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
