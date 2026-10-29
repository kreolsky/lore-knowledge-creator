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

from db import create_record


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


# ─── Migration: reference_sort_keys_backfill ─────────────────────────────────


@pytest.mark.asyncio
async def test_reference_sort_keys_backfill(client, test_db, project_with_doc):
    """Two ref groups with NONE keys → keys in updated_at DESC order per group;
    a second run changes nothing; the DOC backfill order (title ASC) is
    unchanged."""
    pid, idx_id, _ = project_with_doc
    from db import get_db

    db = await get_db()
    # Host doc for the second group (raw row, keyed — not under test).
    d2 = "backfill-host-d2"
    await create_record("documents", d2, {
        "project_id": pid, "parent_id": idx_id, "title": "Host2",
        "content": "", "path": "backfill-host2.md", "is_index": False,
        "sort_key": "zz",
    })

    async def raw_ref(rid, host, updated_at):
        await create_record("documents", rid, {
            "project_id": pid, "parent_id": host, "title": rid,
            "content": "", "path": f"_ref/{rid}.md", "is_reference": True,
            "media_type": "markdown",
        })
        await db.query(
            "UPDATE type::record('documents', $id) SET updated_at = <datetime>$t",
            {"id": rid, "t": updated_at},
        )

    # Group 1 (index): newest, middle, oldest. Group 2 (d2): oldest, newest.
    await raw_ref("bf-i-new", idx_id, "2026-03-03T00:00:00Z")
    await raw_ref("bf-i-mid", idx_id, "2026-02-02T00:00:00Z")
    await raw_ref("bf-i-old", idx_id, "2026-01-01T00:00:00Z")
    await raw_ref("bf-d-old", d2, "2026-01-01T00:00:00Z")
    await raw_ref("bf-d-new", d2, "2026-03-03T00:00:00Z")

    from sort_keys import assign_sort_keys_to_none_rows

    from migrations.migrate_reference_sort_keys_backfill import (
        _migrate_reference_sort_keys_backfill,
    )

    await _migrate_reference_sort_keys_backfill(db)

    async def key_of(rid):
        rows = await db.query(
            "SELECT sort_key FROM type::record('documents', $id)", {"id": rid},
        )
        return rows[0]["sort_key"]

    # updated_at DESC = newest first = smallest key first (newest-first base).
    assert await key_of("bf-i-new") < await key_of("bf-i-mid") < await key_of("bf-i-old")
    assert await key_of("bf-d-new") < await key_of("bf-d-old")

    # Second run: idempotent (NONE rows only) — nothing changes.
    before = {rid: await key_of(rid) for rid in
              ("bf-i-new", "bf-i-mid", "bf-i-old", "bf-d-new", "bf-d-old")}
    await _migrate_reference_sort_keys_backfill(db)
    after = {rid: await key_of(rid) for rid in before}
    assert after == before

    # Doc backfill order (title ASC) unchanged by the kind parameter.
    for did, title in (("bf-doc-b", "B doc"), ("bf-doc-a", "A doc")):
        await create_record("documents", did, {
            "project_id": pid, "parent_id": None, "title": title,
            "content": "", "path": f"backfill-{did}.md", "is_index": False,
        })
    await assign_sort_keys_to_none_rows(db)
    assert await key_of("bf-doc-a") < await key_of("bf-doc-b")
