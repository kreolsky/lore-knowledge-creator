"""Tests for project search endpoint — documents + references."""

import pytest


@pytest.fixture
async def search_project(client, admin_user):
    """Create a project with documents and references containing known searchable strings."""
    uid, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post("/api/projects", json={"name": "Search Test"}, cookies=cookies)
    pid = resp.json()["project_id"]

    # Documents
    await client.post("/api/documents", json={
        "project_id": pid, "title": "Valdris Geography",
        "content": "The aetherium crystals glow beneath the Thunderpeak mountains. storm storm storm.",
    }, cookies=cookies)

    await client.post("/api/documents", json={
        "project_id": pid, "title": "Magic System",
        "content": "Storm channeling requires magic. One storm here.",
    }, cookies=cookies)

    # References
    await client.post("/api/references", json={
        "project_id": pid, "title": "Climate Notes", "media_type": "markdown",
        "content": "geological surveys needed for magic analysis",
    }, cookies=cookies)

    return pid, uid, token


@pytest.mark.asyncio
async def test_search_returns_both_docs_and_refs(client, search_project):
    pid, _, token = search_project
    resp = await client.get(f"/api/projects/{pid}/search?q=magic", cookies={"lore_session": token})
    results = resp.json()["results"]
    has_doc = any("document_id" in r and "reference_id" not in r for r in results)
    has_ref = any("reference_id" in r for r in results)
    assert has_doc and has_ref


@pytest.mark.asyncio
async def test_search_sorted_by_match_count(client, search_project):
    pid, _, token = search_project
    resp = await client.get(f"/api/projects/{pid}/search?q=storm", cookies={"lore_session": token})
    results = resp.json()["results"]
    assert len(results) >= 2
    assert results[0]["match_count"] >= results[1]["match_count"]


@pytest.mark.asyncio
async def test_search_snippet_structure(client, search_project):
    pid, _, token = search_project
    resp = await client.get(f"/api/projects/{pid}/search?q=aetherium", cookies={"lore_session": token})
    snippet = resp.json()["results"][0]["snippet"]
    assert snippet is not None
    assert "before" in snippet
    assert "match" in snippet
    assert "after" in snippet
    assert snippet["match"].lower() == "aetherium"


@pytest.mark.asyncio
async def test_search_requires_project_access(client, search_project, regular_user):
    pid, _, _ = search_project
    _, other_token = regular_user
    resp = await client.get(f"/api/projects/{pid}/search?q=storm", cookies={"lore_session": other_token})
    assert resp.status_code in (403, 404)  # 404 when project not visible, 403 when visible but no access
