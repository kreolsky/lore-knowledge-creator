"""API tests for semantic search endpoint — unified vector search over doc_chunks."""


async def test_semantic_search_requires_auth(client, project_with_doc):
    """No cookie → 401."""
    pid, _, _ = project_with_doc
    resp = await client.get(f"/api/projects/{pid}/semantic-search?q=test")
    assert resp.status_code == 401


async def test_semantic_search_requires_project_access(client, regular_user, project_with_doc):
    """User without project membership → 403."""
    pid, _, _ = project_with_doc
    _, token = regular_user
    resp = await client.get(
        f"/api/projects/{pid}/semantic-search?q=test",
        cookies={"lore_session": token},
    )
    assert resp.status_code in (403, 404)
