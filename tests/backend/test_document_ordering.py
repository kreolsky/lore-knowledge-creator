"""Integration tests for document sibling ordering (sort_key).

Base order is newest-first (new docs land at the TOP of their sibling group);
manual reorder via PATCH /api/documents/{id}/reorder takes priority and is
backend-authoritative (the client sends `after_id`, the server computes the key).
"""


import pytest


async def _create(client, cookies, pid, title, parent_id=None):
    body = {"project_id": pid, "title": title}
    if parent_id is not None:
        body["parent_id"] = parent_id
    resp = await client.post("/api/documents", json=body, cookies=cookies)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_reorder_between_equal_keys_returns_409(client, admin_user, project_with_doc, test_db):
    """Degenerate bounds (two siblings sharing a key) yield a clean 409, not a 500."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    # Isolate A/B/Mover under a dedicated parent. The session-scoped test_db accumulates
    # root docs from other tests; a stray root sibling slipping between A and B would make
    # the bounds non-degenerate (a legitimate 200), masking the 409 path this test guards.
    parent = await _create(client, cookies, pid, "EqualKeysGroup")
    group = parent["document_id"]
    a = await _create(client, cookies, pid, "A", parent_id=group)
    b = await _create(client, cookies, pid, "B", parent_id=group)
    mover = await _create(client, cookies, pid, "Mover", parent_id=group)
    # Force A and B to share an identical sort_key (only possible via a concurrent-write race).
    for d in (a, b):
        await test_db.query(
            "UPDATE type::record('documents', $id) SET sort_key = 'aQ'",
            {"id": d["document_id"]},
        )
    # sibling_rows tie-breaks equal keys by id ASC, so dropping after the lower-id sibling
    # guarantees the next sibling shares the key (degenerate bounds) regardless of DB order.
    lower_id = min(a["document_id"], b["document_id"])
    resp = await client.patch(
        f"/api/documents/{mover['document_id']}/reorder",
        json={"after_id": lower_id},
        cookies=cookies,
    )
    assert resp.status_code == 409, resp.text


# ─── Cross-level drag: reorder with parent_id (the tree drop contract) ────────


@pytest.mark.asyncio
async def test_reorder_with_parent_own_descendant_rejected(
    client, admin_user, project_with_doc,
):
    """parent_id naming the doc's own descendant is a cycle ⇒ 400."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc = await _create(client, cookies, pid, "D")
    child = await _create(client, cookies, pid, "C", parent_id=doc["document_id"])

    resp = await client.patch(
        f"/api/documents/{doc['document_id']}/reorder",
        json={"parent_id": child["document_id"]},
        cookies=cookies,
    )
    assert resp.status_code == 400, resp.text
