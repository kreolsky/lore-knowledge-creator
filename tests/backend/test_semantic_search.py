"""API tests for semantic search endpoint — unified vector search over doc_chunks."""

from unittest.mock import AsyncMock, patch

from retrieval import RetrievalHit, RetrievalResult


def _make_hits(*hits):
    return RetrievalResult(hits=list(hits))


async def test_semantic_search_returns_hits(client, admin_user, project_with_doc):
    """Cookie auth + mocked retrieval → 200 with hits array."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    fake_result = _make_hits(
        RetrievalHit(
            kind="document", parent_id="d1", parent_title="Doc One",
            heading="Intro", snippet="some text here", offset_start=0, offset_end=14, score=0.92,
        ),
    )
    with patch("retrieval.retrieve_context", new_callable=AsyncMock, return_value=fake_result):
        resp = await client.get(
            f"/api/projects/{pid}/semantic-search?q=magic",
            cookies={"lore_session": token},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "magic"
    assert len(data["hits"]) == 1
    assert data["hits"][0]["kind"] == "document"
    assert data["hits"][0]["score"] == 0.92


async def test_semantic_search_empty_results(client, admin_user, project_with_doc):
    """Cookie auth + no hits → 200 with empty hits array."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    with patch("retrieval.retrieve_context", new_callable=AsyncMock, return_value=RetrievalResult(hits=[])):
        resp = await client.get(
            f"/api/projects/{pid}/semantic-search?q=obscure+topic",
            cookies={"lore_session": token},
        )
    assert resp.status_code == 200
    assert resp.json()["hits"] == []


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


async def test_semantic_search_min_query_length(client, admin_user, project_with_doc):
    """Single character query → 422 validation error."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.get(
        f"/api/projects/{pid}/semantic-search?q=a",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422


async def test_semantic_search_embedding_not_configured(client, admin_user, project_with_doc):
    """Retrieval returns not_configured error → 503."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    with patch("retrieval.retrieve_context", new_callable=AsyncMock, return_value=RetrievalResult(error="not_configured")):
        resp = await client.get(
            f"/api/projects/{pid}/semantic-search?q=test",
            cookies={"lore_session": token},
        )
    assert resp.status_code == 503


async def test_semantic_search_k_override(client, admin_user, project_with_doc):
    """Agent passes ?k=3 → top_k_docs=3, top_k_refs=3 forwarded to retrieve_context."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    with patch("retrieval.retrieve_context", new_callable=AsyncMock, return_value=RetrievalResult(hits=[])) as mock:
        resp = await client.get(
            f"/api/projects/{pid}/semantic-search?q=test&k=3",
            cookies={"lore_session": token},
        )
    assert resp.status_code == 200
    call_kwargs = mock.call_args[1]
    assert call_kwargs["top_k_docs"] == 3
    assert call_kwargs["top_k_refs"] == 3


async def test_semantic_search_top_k_asymmetric_override(client, admin_user, project_with_doc):
    """Agent passes ?k=10&top_k_docs=20&top_k_refs=5 → asymmetric per-kind limits."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    with patch("retrieval.retrieve_context", new_callable=AsyncMock, return_value=RetrievalResult(hits=[])) as mock:
        resp = await client.get(
            f"/api/projects/{pid}/semantic-search?q=test&k=10&top_k_docs=20&top_k_refs=5",
            cookies={"lore_session": token},
        )
    assert resp.status_code == 200
    call_kwargs = mock.call_args[1]
    assert call_kwargs["top_k_docs"] == 20
    assert call_kwargs["top_k_refs"] == 5
