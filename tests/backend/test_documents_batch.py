"""Integration tests for POST /api/documents/batch — the unified content batch.

SYSTEM: documents-batch-tests — single projected SELECT resolves N transcluded
doc/reference ids for one project read check (plan: reference-loading-acceleration).

Covers the Phase-1 contract:
- returns content for valid same-project ids (mix of regular docs + references);
- applies live collab-session content over the stale DB row (merge_live_content);
- omits deleted / missing / unknown ids silently (no 404);
- filters cross-project ids out of the result (does NOT 404 the whole batch);
- 403 when the user lacks project read on the resolved project;
- never leaks ydoc_state (SQL projection excludes it);
- fields=content (default) omits headings;
- enforces the ids length cap (<=200).
"""

import uuid

import pytest


@pytest.mark.asyncio
async def test_batch_returns_content_for_same_project_docs_and_refs(
    client, admin_user, project_with_doc,
):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    # One regular doc + one markdown reference, both in the same project.
    doc_resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Doc", "content": "doc body"},
        cookies=cookies,
    )
    doc_id = doc_resp.json()["document_id"]
    ref_resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "is_reference": True, "media_type": "markdown",
            "parent_id": idx_id, "title": "Ref", "content": "ref body",
        },
        cookies=cookies,
    )
    ref_id = ref_resp.json()["document_id"]

    resp = await client.post(
        "/api/documents/batch",
        json={"ids": [doc_id, ref_id]},
        cookies=cookies,
    )
    assert resp.status_code == 200
    items = {it["document_id"]: it for it in resp.json()["items"]}
    assert set(items) == {doc_id, ref_id}
    assert items[doc_id]["content"] == "doc body"
    assert items[ref_id]["content"] == "ref body"
    assert items[ref_id]["is_reference"] is True


@pytest.mark.asyncio
async def test_batch_applies_live_session_content(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Live", "content": "stale db body"},
        cookies=cookies,
    )
    doc_id = doc_resp.json()["document_id"]

    # merge_live_content prefers the live Y.Doc over the ~1s-lagging flush.
    # Patch get_active_session so a live session is reported ONLY for this doc.
    from types import SimpleNamespace
    from unittest.mock import patch

    live = SimpleNamespace(content="live body")

    def fake_active(_entity_type: str, entity_id: str):
        return live if entity_id == doc_id else None

    with patch("collab.registry.get_active_session", side_effect=fake_active):
        resp = await client.post(
            "/api/documents/batch",
            json={"ids": [doc_id]},
            cookies=cookies,
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["document_id"] == doc_id
    assert items[0]["content"] == "live body"


@pytest.mark.asyncio
async def test_batch_omits_deleted_missing_unknown(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    live = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Alive", "content": "ok"},
        cookies=cookies,
    )
    live_id = live.json()["document_id"]
    deleted = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Gone", "content": "bye"},
        cookies=cookies,
    )
    deleted_id = deleted.json()["document_id"]
    await client.delete(f"/api/documents/{deleted_id}", cookies=cookies)
    unknown_id = str(uuid.uuid4())

    resp = await client.post(
        "/api/documents/batch",
        json={"ids": [live_id, deleted_id, unknown_id]},
        cookies=cookies,
    )
    assert resp.status_code == 200
    ids = {it["document_id"] for it in resp.json()["items"]}
    assert ids == {live_id}


@pytest.mark.asyncio
async def test_batch_filters_cross_project_ids(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    same = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Same", "content": "a"},
        cookies=cookies,
    )
    same_id = same.json()["document_id"]
    # A second project owned by the same admin — its doc must NOT leak into the
    # first project's batch result.
    other_proj = await client.post(
        "/api/projects", json={"name": "Other"}, cookies=cookies,
    )
    other_pid = other_proj.json()["project_id"]
    other_doc = await client.post(
        "/api/documents",
        json={"project_id": other_pid, "title": "Other", "content": "b"},
        cookies=cookies,
    )
    other_id = other_doc.json()["document_id"]

    resp = await client.post(
        "/api/documents/batch",
        json={"ids": [same_id, other_id]},
        cookies=cookies,
    )
    assert resp.status_code == 200
    ids = {it["document_id"] for it in resp.json()["items"]}
    assert ids == {same_id}


@pytest.mark.asyncio
async def test_batch_403_without_project_read(
    client, admin_user, regular_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    _, regular_token = regular_user
    admin_cookies = {"lore_session": admin_token}
    doc = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Priv", "content": "secret"},
        cookies=admin_cookies,
    )
    doc_id = doc.json()["document_id"]
    # regular_user is NOT a member of admin's private project → no read access.
    resp = await client.post(
        "/api/documents/batch",
        json={"ids": [doc_id]},
        cookies={"lore_session": regular_token},
    )
    assert resp.status_code in (403, 404)


@pytest.mark.asyncio
async def test_batch_never_returns_ydoc_state(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Bin", "content": "x"},
        cookies=cookies,
    )
    doc_id = doc.json()["document_id"]
    resp = await client.post(
        "/api/documents/batch",
        json={"ids": [doc_id]},
        cookies=cookies,
    )
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert "ydoc_state" not in item


@pytest.mark.asyncio
async def test_batch_content_omits_headings(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "H", "content": "# Title\nbody"},
        cookies=cookies,
    )
    doc_id = doc.json()["document_id"]
    resp = await client.post(
        "/api/documents/batch",
        json={"ids": [doc_id]},
        cookies=cookies,
    )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert "headings" not in item


@pytest.mark.asyncio
async def test_batch_all_missing_returns_404(client, admin_user, project_with_doc):
    """INVARIANT(security): an all-missing batch 404s (NOT 200+[]) so the only
    distinguishable outcomes match the single-doc uniform-404 posture — collapsing
    'all missing' into 'no access' prevents a 3-state existence/access oracle."""
    _, token = admin_user
    cookies = {"lore_session": token}
    unknown_a = str(uuid.uuid4())
    unknown_b = str(uuid.uuid4())
    resp = await client.post(
        "/api/documents/batch",
        json={"ids": [unknown_a, unknown_b]},
        cookies=cookies,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_batch_enforces_ids_cap(client, admin_user, project_with_doc):
    _, token = admin_user
    cookies = {"lore_session": token}
    too_many = [str(uuid.uuid4()) for _ in range(201)]
    resp = await client.post(
        "/api/documents/batch",
        json={"ids": too_many},
        cookies=cookies,
    )
    assert resp.status_code == 422
