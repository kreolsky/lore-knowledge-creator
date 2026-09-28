"""GET /api/documents/open/{document_id} — one-shot cold-open bundle
(plan "public-document-ids").

The bare-URL cold path (/docs/<id>) no longer carries project_id in the URL.
DocumentPage fires document + references + note-sessions CONCURRENTLY (its ARCH
note: the prior waterfall committed the document ref-less and flickered). This
endpoint returns the whole bundle in one RTT so the page can commit the document
together with its restored reference in a single render. In-app nav keeps the
per-id calls (project_id is already in the store) — entry-path-only addition.
"""
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_open_returns_bundle_shape(client, admin_user, project_with_doc):
    """GET /open/{id} → {document, references, note_sessions, project}."""
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user

    resp = await client.get(
        f"/api/documents/open/{idx_id}", cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"]["document_id"] == idx_id
    assert body["project"]["project_id"] == pid
    assert isinstance(body["references"], list)
    assert isinstance(body["note_sessions"], list)


@pytest.mark.asyncio
async def test_open_requires_auth(client, project_with_doc):
    """No cookie → 401 (get_current_user gate)."""
    _, idx_id, _ = project_with_doc
    resp = await client.get(f"/api/documents/open/{idx_id}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_open_unknown_doc_404(client, admin_user, project_with_doc):
    """Unknown id → uniform 404."""
    _, admin_token = admin_user
    resp = await client.get(
        f"/api/documents/open/{uuid4()}", cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_open_includes_document_references(client, admin_user, project_with_doc):
    """The bundle carries the document's references (transclusion seed)."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc = await client.post(
        "/api/documents", json={"project_id": pid, "title": "Doc", "content": "body"},
        cookies={"lore_session": admin_token},
    )
    assert doc.status_code == 200, doc.text
    doc_id = doc.json()["document_id"]
    ref = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": doc_id, "title": "Ref",
              "media_type": "markdown", "content": "ref body"},
        cookies={"lore_session": admin_token},
    )
    assert ref.status_code == 200, ref.text

    resp = await client.get(
        f"/api/documents/open/{doc_id}", cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    refs = resp.json()["references"]
    assert len(refs) >= 1, f"bundle must include the doc's references, got {refs}"


@pytest.mark.asyncio
async def test_open_project_carries_my_access(client, admin_user, project_with_doc):
    """The bundle's project carries my_access — the store seeds accessLevel from it.

    Without it the frontend keeps its 'full' default until the corrective
    GET /api/projects/{id} lands, so a readonly member briefly sees edit
    affordances on the bare /docs/<id> cold path.
    """
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user

    resp = await client.get(
        f"/api/documents/open/{idx_id}", cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    project = resp.json()["project"]
    # Derived, not a literal: the same value GET /api/projects/{id} reports.
    canonical = await client.get(
        f"/api/projects/{pid}", cookies={"lore_session": admin_token},
    )
    assert canonical.status_code == 200, canonical.text
    assert project["my_access"] == canonical.json()["project"]["my_access"]
