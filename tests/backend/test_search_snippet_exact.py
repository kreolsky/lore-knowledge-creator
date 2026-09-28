"""UI-route fulltext search: fan-out guard, anchored snippets, exact toggle.

`/api/projects/{id}/search` (routes/projects.py) — the plan
fulltext-snippet-exact-toggle: SurrealDB FULLTEXT matches ANY token (OR), so the
raw multi-word query fan-outs to docs sharing a stop word ("0"-badge cards);
punctuation queries (`ИИ-1.2`) tokenize to sub-3-char garbage. These pin the
fixed contract: operand preprocessing (stop words + <3-char tokens dropped),
anchor-based match_count/snippet (never 0-match cards), unanchorable results
dropped, and the `exact` literal-substring mode.
"""

import pytest


@pytest.fixture
async def fanout_project(client, admin_user):
    """docA carries the real keyword; docB is a stop-word-only body that the
    raw-query FTS OR matched before the fix (count 0, no snippet)."""
    uid, token = admin_user
    cookies = {"lore_session": token}
    resp = await client.post("/api/projects", json={"name": "Fanout Test"}, cookies=cookies)
    pid = resp.json()["project_id"]
    await client.post("/api/documents", json={
        "project_id": pid, "title": "Крепость",
        "content": "Замок стоит на холме над рекой.",
    }, cookies=cookies)
    await client.post("/api/documents", json={
        "project_id": pid, "title": "Пустышка",
        "content": "и",
    }, cookies=cookies)
    return pid, uid, token


@pytest.fixture
async def punct_project(client, admin_user):
    """`ИИ-1.2` tokenizes (\\W+) to ии/1/2 — all sub-3-char, so the default mode
    must degrade to the literal contains scan, not FTS."""
    uid, token = admin_user
    cookies = {"lore_session": token}
    resp = await client.post("/api/projects", json={"name": "Punct Test"}, cookies=cookies)
    pid = resp.json()["project_id"]
    await client.post("/api/documents", json={
        "project_id": pid, "title": "Системный журнал",
        "content": "код ИИ-1.2 активен",
    }, cookies=cookies)
    await client.post("/api/documents", json={
        "project_id": pid, "title": "Заметки",
        "content": "версия 1 и 2",
    }, cookies=cookies)
    return pid, uid, token


@pytest.fixture
async def stem_project(client, admin_user):
    """Same body as test_search_fts: inflected forms of мир (мира/миру)."""
    uid, token = admin_user
    cookies = {"lore_session": token}
    resp = await client.post("/api/projects", json={"name": "Stem Test"}, cookies=cookies)
    pid = resp.json()["project_id"]
    await client.post("/api/documents", json={
        "project_id": pid, "title": "Valdris Geography",
        "content": "История мира и судьба миру принадлежит героям.",
    }, cookies=cookies)
    return pid, uid, token


async def _search(client, pid, token, q, exact=False):
    url = f"/api/projects/{pid}/search?q={q}"
    if exact:
        url += "&exact=true"
    resp = await client.get(url, cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()["results"]


# ─── fan-out guard (default mode) ────────────────────────────────────────────


async def test_stop_word_fanout_kills_garbage_cards(client, fanout_project):
    """`что и замок` → docA present WITH a stem-anchored snippet, docB (body
    `и`, matched only via the raw OR) ABSENT. Before the fix docB returned with
    match_count 0 and no snippet."""
    pid, _, token = fanout_project
    results = await _search(client, pid, token, "что и замок")
    titles = [r["title"] for r in results]
    assert "Крепость" in titles, results
    assert "Пустышка" not in titles, results
    hit = next(r for r in results if r["title"] == "Крепость")
    assert hit["match_count"] >= 1, hit
    assert hit["snippet"] is not None, hit
    window = (hit["snippet"]["before"] + hit["snippet"]["match"] + hit["snippet"]["after"]).lower()
    assert "замок" in window, hit


async def test_punctuation_query_degrades_to_literal_scan(client, punct_project):
    """`ИИ-1.2` (default mode, no exact): every token is sub-3-char → the FTS
    operand is empty → the literal scan runs. Only docA matches; docB
    (`версия 1 и 2`, matched pre-fix via token `1`/`2`) is absent."""
    pid, _, token = punct_project
    results = await _search(client, pid, token, "ИИ-1.2")
    titles = [r["title"] for r in results]
    assert titles == ["Системный журнал"], results
    hit = results[0]
    assert hit["snippet"]["match"].lower() == "ии-1.2", hit


# ─── stem anchors: kept, counted, snippet non-null ───────────────────────────


async def test_stem_hit_kept_with_prefix3_anchor(client, stem_project):
    """q=`миром` (instrumental): the body carries мира/миру, never `миром` —
    the anchor ladder's prefix-3 rung (`мир`) must keep the hit with
    match_count ≥ 1 and a snippet whose match sits INSIDE the inflected form."""
    pid, _, token = stem_project
    results = await _search(client, pid, token, "миром")
    hits = [r for r in results if r["title"] == "Valdris Geography"]
    assert hits, results
    hit = hits[0]
    assert hit["match_count"] >= 1, hit
    assert hit["snippet"] is not None, hit
    assert hit["snippet"]["match"] == "мир", hit


async def test_title_only_hit_is_not_dropped(client, stem_project):
    """q=`val` matches the TITLE only (edgengram): kept, count ≥ 1, snippet
    None is acceptable — the title is already displayed. Guards the drop rule
    against over-dropping title-only hits."""
    pid, _, token = stem_project
    results = await _search(client, pid, token, "val")
    hits = [r for r in results if r["title"] == "Valdris Geography"]
    assert hits, results
    assert hits[0]["match_count"] >= 1, hits[0]


# ─── exact mode (D1: case-insensitive literal substring, no FTS) ─────────────


async def test_exact_mode_is_case_insensitive(client, punct_project):
    pid, _, token = punct_project
    results = await _search(client, pid, token, "ии-1.2", exact=True)
    titles = [r["title"] for r in results]
    assert "Системный журнал" in titles, results
    assert "Заметки" not in titles, results


async def test_exact_mode_every_hit_contains_query_literally(client, punct_project):
    """Matcher-level guard (mirror of the agent D9a test): every exact hit's
    title+content literally contains the lowercased query — exact may never
    gain a matcher (stem/prefix) the literal scan lacks."""
    pid, _, token = punct_project
    from db import get_db

    db = await get_db()
    for q in ["ии-1.2", "версия", "код"]:
        for h in await _search(client, pid, token, q, exact=True):
            rows = await db.query(
                "SELECT title, content FROM documents WHERE meta::id(id) = $id",
                {"id": h["document_id"]},
            )
            row = rows[0] if isinstance(rows, list) and rows else {}
            blob = f"{row.get('title') or ''} {row.get('content') or ''}".lower()
            assert q.lower() in blob, (
                q, h["document_id"], "exact hit must literally contain the query string"
            )


async def test_exact_mode_never_issues_fts_query(client, punct_project, monkeypatch):
    """exact runs the contains scan alone — an @0@ (FTS) statement reaching the
    wire is the regression. The wrapper also raises on @0@ so an accidental FTS
    attempt cannot hide behind the route's fallback."""
    pid, _, token = punct_project
    from db import get_db

    db = await get_db()
    orig_query = db.query
    seen: list[str] = []

    async def recording(sql, *args, **kwargs):
        seen.append(sql)
        if "@0@" in sql:
            raise RuntimeError("simulated: FTS must not run in exact mode")
        return await orig_query(sql, *args, **kwargs)

    monkeypatch.setattr(db, "query", recording)
    results = await _search(client, pid, token, "ИИ-1.2", exact=True)
    assert any(r["title"] == "Системный журнал" for r in results), results
    assert not any("@0@" in s for s in seen), (
        "exact mode issued an FTS statement", [s for s in seen if "@0@" in s]
    )


# ─── under_document_id ("In this document") — subtree scope ──────────────────


@pytest.fixture
async def subtree_project(client, admin_user):
    """parent ← child (both carry the keyword) + an outsider doc that also carries
    it — filtering must be by SCOPE, not by content."""
    uid, token = admin_user
    cookies = {"lore_session": token}
    resp = await client.post("/api/projects", json={"name": "Subtree Test"}, cookies=cookies)
    pid = resp.json()["project_id"]
    parent = (await client.post("/api/documents", json={
        "project_id": pid, "title": "Родитель", "content": "Замок на холме.",
    }, cookies=cookies)).json()
    await client.post("/api/documents", json={
        "project_id": pid, "title": "Потомок", "content": "Замок внутри стен.",
        "parent_id": parent["document_id"],
    }, cookies=cookies)
    await client.post("/api/documents", json={
        "project_id": pid, "title": "Чужак", "content": "Замок в другом месте.",
    }, cookies=cookies)
    return pid, parent["document_id"], uid, token


async def test_under_document_id_scopes_to_subtree(client, subtree_project):
    """`under_document_id` narrows to root + descendants (same semantics as
    semantic-search): parent and child stay, the outsider drops."""
    pid, parent_id, _, token = subtree_project
    results = await _search(client, pid, token, "замок")
    assert {r["title"] for r in results} == {"Родитель", "Потомок", "Чужак"}, results

    resp = await client.get(
        f"/api/projects/{pid}/search?q=замок&under_document_id={parent_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    titles = {r["title"] for r in resp.json()["results"]}
    assert titles == {"Родитель", "Потомок"}, titles


async def test_under_document_id_unknown_root_404(client, subtree_project):
    """Uniform 404 on an unknown root — mirrors semantic-search (no existence
    oracle; never silently degrades to a whole-project search)."""
    pid, _, _, token = subtree_project
    resp = await client.get(
        f"/api/projects/{pid}/search?q=замок&under_document_id=no-such-doc",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404, resp.text
