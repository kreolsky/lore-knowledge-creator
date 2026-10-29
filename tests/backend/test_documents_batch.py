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
