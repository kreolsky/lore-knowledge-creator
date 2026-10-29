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
