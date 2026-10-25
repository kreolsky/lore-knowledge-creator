"""PATCH /api/documents/{id} parent contract: sent-null vs absent parent_id.

An explicitly SENT `"parent_id": null` moves the document to the project root,
exactly like `""` — the same model_fields_set contract /reorder already uses.
An ABSENT `parent_id` (e.g. a title-only PATCH) must leave the parent alone.
Read-back is GET /api/documents/{id} (the row, not the tree).
"""

import pytest
import pytest_asyncio


async def _mk_doc(client, pid, token, *, title, parent_id=None):
    r = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title, "parent_id": parent_id},
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


async def _patch(client, token, doc_id, body):
    return await client.patch(
        f"/api/documents/{doc_id}", json=body, cookies={"lore_session": token},
    )


async def _parent_via_get(client, token, doc_id):
    # Root docs carry NO parent_id key at all (SurrealDB NONE drops the field),
    # so absent key and null both read as "at root" — exactly what a client sees.
    r = await client.get(
        f"/api/documents/{doc_id}", cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    return r.json().get("parent_id")


@pytest_asyncio.fixture
async def nested(client, admin_user, project_with_doc):
    """Index → A → B (B is the nested doc under test)."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    a = await _mk_doc(client, pid, token, title="A", parent_id=idx_id)
    b = await _mk_doc(client, pid, token, title="B", parent_id=a)
    return token, a, b


@pytest.mark.asyncio
async def test_sent_null_parent_moves_to_root(client, nested):
    token, a, b = nested
    assert await _parent_via_get(client, token, b) == a
    resp = await _patch(client, token, b, {"parent_id": None})
    assert resp.status_code == 200, resp.text
    assert await _parent_via_get(client, token, b) is None


@pytest.mark.asyncio
async def test_absent_parent_leaves_parent_alone(client, nested):
    token, a, b = nested
    resp = await _patch(client, token, b, {"title": "renamed"})
    assert resp.status_code == 200, resp.text
    assert await _parent_via_get(client, token, b) == a


@pytest.mark.asyncio
async def test_empty_string_parent_still_moves_to_root(client, nested):
    token, _, b = nested
    resp = await _patch(client, token, b, {"parent_id": ""})
    assert resp.status_code == 200, resp.text
    assert await _parent_via_get(client, token, b) is None
