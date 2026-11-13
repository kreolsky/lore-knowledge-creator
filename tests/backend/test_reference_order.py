"""Reference manual order — persisted, shared sort_key per (project, parent, ref) group.

Contract:
  - every live reference row gets a sort_key at birth, at the TOP of its host's
    reference group (key_before = newest-first);
  - reorder serves BOTH kinds through the existing PATCH /api/documents/{id}/reorder
    (after_id must name a LIVE ref of the SAME group; archived refs are refused on
    either side);
  - a ref reorder does NOT bump updated_at (position is not an edit);
  - re-hosting (PATCH /api/references/{id} document_id, or a tree move) mints the
    top key of the NEW host's ref group and the move events carry it;
  - LIST order inside a tier is (sort_key, id) ASC, tiers and the archived sink
    unchanged; a content edit no longer moves a ref;
  - the ref key space INCLUDES archived refs (a group holding only archived refs
    still mints a distinct top key);
  - mixed readers (agent_config_load, structure_exec) keep refs in the band they
    occupy today (RED-first measurement below).
"""


import pytest


async def _mk_doc(client, token, pid, title, parent_id=None):
    r = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title, "parent_id": parent_id},
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


async def _mk_ref(client, token, pid, host, title):
    r = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": host, "title": title,
              "media_type": "markdown", "content": "body"},
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    return r.json()["reference_id"]


async def _list_refs(client, token, scope, *, archived=True):
    r = await client.get(
        f"/api/references?{scope}&include_archived={str(archived).lower()}",
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _reorder(client, token, doc_id, after_id):
    return await client.patch(
        f"/api/documents/{doc_id}/reorder",
        json={"after_id": after_id},
        cookies={"lore_session": token},
    )


async def _row(test_db, ref_id):
    rows = await test_db.query(
        "SELECT meta::id(id) AS id, parent_id, sort_key, updated_at, archived "
        "FROM type::record('documents', $id)",
        {"id": ref_id},
    )
    assert rows, ref_id
    row = rows[0]
    parent = row.get("parent_id")
    row["parent"] = str(parent).split(":")[-1] if parent else None
    return row


# ─── Reorder semantics: top / after X / updated_at stable ────────────────────


@pytest.mark.asyncio
async def test_ref_reorder_top_after_and_updated_at_stable(
    client, admin_user, project_with_doc, test_db,
):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    d = await _mk_doc(client, token, pid, "D", parent_id=idx_id)
    r1 = await _mk_ref(client, token, pid, d, "r1")
    r2 = await _mk_ref(client, token, pid, d, "r2")
    r3 = await _mk_ref(client, token, pid, d, "r3")
    # creation order (top-first): r3, r2, r1

    before = (await _row(test_db, r1))["updated_at"]
    resp = await _reorder(client, token, r1, r3)
    assert resp.status_code == 200, resp.text
    listed = [r["reference_id"] for r in await _list_refs(client, token, f"document_id={d}")]
    assert listed[:3] == [r3, r1, r2], listed
    row = await _row(test_db, r1)
    assert row["updated_at"] == before, "a ref reorder must not bump updated_at"
    assert row["parent"] == d

    resp = await _reorder(client, token, r1, None)
    assert resp.status_code == 200, resp.text
    listed = [r["reference_id"] for r in await _list_refs(client, token, f"document_id={d}")]
    assert listed[:3] == [r1, r3, r2], listed
    assert (await _row(test_db, r1))["parent"] == d
