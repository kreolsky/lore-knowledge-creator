"""GET /api/documents/open/{document_id} — one-shot cold-open bundle
(plan "public-document-ids").

The bare-URL cold path (/docs/<id>) no longer carries project_id in the URL.
DocumentPage fires document + references + note-sessions CONCURRENTLY (its ARCH
note: the prior waterfall committed the document ref-less and flickered). This
endpoint returns the whole bundle in one RTT so the page can commit the document
together with its restored reference in a single render. In-app nav keeps the
per-id calls (project_id is already in the store) — entry-path-only addition.
"""

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
