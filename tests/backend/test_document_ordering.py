"""Integration tests for document sibling ordering (sort_key).

Base order is newest-first (new docs land at the TOP of their sibling group);
manual reorder via PATCH /api/documents/{id}/reorder takes priority and is
backend-authoritative (the client sends `after_id`, the server computes the key).
"""


import pytest
from emit_recorder import EmitRecorder


async def _create(client, cookies, pid, title, parent_id=None):
    body = {"project_id": pid, "title": title}
    if parent_id is not None:
        body["parent_id"] = parent_id
    resp = await client.post("/api/documents", json=body, cookies=cookies)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _sibling_order(client, cookies, pid, parent_id=None):
    """Return document_ids of non-index docs under `parent_id`, in returned order."""
    resp = await client.get(f"/api/projects/{pid}", cookies=cookies)
    assert resp.status_code == 200, resp.text
    docs = resp.json()["documents"]
    return [
        d["document_id"]
        for d in docs
        if not d.get("is_index") and d.get("parent_id") == parent_id
    ]


@pytest.mark.asyncio
async def test_create_assigns_sort_key(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc = await _create(client, cookies, pid, "First")
    assert doc.get("sort_key")


@pytest.mark.asyncio
async def test_create_prepends_newest_first(client, admin_user, project_with_doc):
    """Three root docs created A→B→C must list as C, B, A (newest on top)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    a = await _create(client, cookies, pid, "A")
    b = await _create(client, cookies, pid, "B")
    c = await _create(client, cookies, pid, "C")
    order = await _sibling_order(client, cookies, pid)
    assert order == [c["document_id"], b["document_id"], a["document_id"]]


@pytest.mark.asyncio
async def test_reorder_after_sibling(client, admin_user, project_with_doc):
    """Start C,B,A → place C after A (bottom) → B,A,C."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    a = await _create(client, cookies, pid, "A")
    b = await _create(client, cookies, pid, "B")
    c = await _create(client, cookies, pid, "C")

    resp = await client.patch(
        f"/api/documents/{c['document_id']}/reorder",
        json={"after_id": a["document_id"]},
        cookies=cookies,
    )
    assert resp.status_code == 200, resp.text
    order = await _sibling_order(client, cookies, pid)
    assert order == [b["document_id"], a["document_id"], c["document_id"]]


@pytest.mark.asyncio
async def test_reorder_to_top(client, admin_user, project_with_doc):
    """after_id=null places the doc at the top of its group."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    a = await _create(client, cookies, pid, "A")
    b = await _create(client, cookies, pid, "B")  # order: B, A

    resp = await client.patch(
        f"/api/documents/{a['document_id']}/reorder",
        json={"after_id": None},
        cookies=cookies,
    )
    assert resp.status_code == 200, resp.text
    order = await _sibling_order(client, cookies, pid)
    assert order == [a["document_id"], b["document_id"]]


@pytest.mark.asyncio
async def test_reorder_rejects_cross_parent_after_id(client, admin_user, project_with_doc):
    """after_id pointing to a doc in a different sibling group is rejected."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    parent = await _create(client, cookies, pid, "Parent")
    child = await _create(client, cookies, pid, "Child", parent_id=parent["document_id"])
    root = await _create(client, cookies, pid, "Root")

    resp = await client.patch(
        f"/api/documents/{root['document_id']}/reorder",
        json={"after_id": child["document_id"]},
        cookies=cookies,
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_reparent_prepends_to_new_group(client, admin_user, project_with_doc):
    """Re-parenting a doc places it at the TOP of the new sibling group."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    parent = await _create(client, cookies, pid, "Parent")
    c1 = await _create(client, cookies, pid, "C1", parent_id=parent["document_id"])
    c2 = await _create(client, cookies, pid, "C2", parent_id=parent["document_id"])
    mover = await _create(client, cookies, pid, "Mover")

    resp = await client.patch(
        f"/api/documents/{mover['document_id']}",
        json={"parent_id": parent["document_id"]},
        cookies=cookies,
    )
    assert resp.status_code == 200, resp.text
    order = await _sibling_order(client, cookies, pid, parent_id=parent["document_id"])
    # newest-first within the parent: C2, C1 then mover prepended on top
    assert order[0] == mover["document_id"]
    assert set(order) == {mover["document_id"], c1["document_id"], c2["document_id"]}


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


@pytest.mark.asyncio
async def test_reorder_emits_event(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    a = await _create(client, cookies, pid, "A")
    b = await _create(client, cookies, pid, "B")

    with EmitRecorder.active() as rec:
        resp = await client.patch(
            f"/api/documents/{b['document_id']}/reorder",
            json={"after_id": a["document_id"]},
            cookies=cookies,
        )
    captured = rec.calls
    assert resp.status_code == 200, resp.text
    events = [e for e in captured if e[0] == "document_reordered"]
    assert events, f"document_reordered not emitted; got {[e[0] for e in captured]}"
    payload = events[0][1]
    assert payload["document_id"] == b["document_id"]
    assert payload.get("sort_key")
