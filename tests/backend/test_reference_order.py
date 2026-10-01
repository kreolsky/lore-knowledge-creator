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

from uuid import uuid4

import pytest
from emit_recorder import EmitRecorder

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


async def _add_member(uid, pid, level):
    await create_record("project_members", str(uuid4()), {
        "project_id": pid, "user_id": uid, "access_level": level,
    })


# ─── Creation: new ref at the TOP of its group; kinds never interact ─────────


@pytest.mark.asyncio
async def test_new_ref_lands_at_top_of_its_group(client, admin_user, project_with_doc, test_db):
    """A new ref's key sorts below every live ref key in the same group (listed
    first in the tier); a new child DOC does not move the refs and a new ref
    does not move the child docs."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    d = await _mk_doc(client, token, pid, "D", parent_id=idx_id)
    r1 = await _mk_ref(client, token, pid, d, "r1")
    r2 = await _mk_ref(client, token, pid, d, "r2")
    r3 = await _mk_ref(client, token, pid, d, "r3")

    keys = {rid: (await _row(test_db, rid))["sort_key"] for rid in (r1, r2, r3)}
    assert all(isinstance(k, str) and k for k in keys.values()), keys
    assert keys[r3] < keys[r2] < keys[r1], "newest ref must hold the smallest key"

    listed = [r["reference_id"] for r in await _list_refs(client, token, f"document_id={d}")]
    assert listed[:3] == [r3, r2, r1], listed

    # A new child DOC under D: the ref group's keys are untouched…
    c = await _mk_doc(client, token, pid, "C", parent_id=d)
    keys_after_doc = {rid: (await _row(test_db, rid))["sort_key"] for rid in (r1, r2, r3)}
    assert keys_after_doc == keys
    # …and the doc's own key is untouched by the refs above.
    doc_key = (await _row(test_db, c))["sort_key"]
    r4 = await _mk_ref(client, token, pid, d, "r4")
    assert (await _row(test_db, c))["sort_key"] == doc_key
    assert (await _row(test_db, r4))["sort_key"] < keys[r3]


# ─── LIST: tiers fixed, key order inside a tier, edits do not move ───────────


@pytest.mark.asyncio
async def test_list_tiers_key_order_edit_does_not_move(
    client, admin_user, project_with_doc, test_db,
):
    """LIST for child C: C's refs, then parent's, then index's; inside each tier
    strictly by sort_key ASC; a content edit of an old ref does NOT change its
    position."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    p = await _mk_doc(client, token, pid, "P", parent_id=idx_id)
    c = await _mk_doc(client, token, pid, "C", parent_id=p)
    c1 = await _mk_ref(client, token, pid, c, "c1")
    c2 = await _mk_ref(client, token, pid, c, "c2")
    p1 = await _mk_ref(client, token, pid, p, "p1")
    p2 = await _mk_ref(client, token, pid, p, "p2")
    i1 = await _mk_ref(client, token, pid, idx_id, "i1")

    # Non-creation order inside C's tier: drag the OLDEST ref (c1) to the top.
    resp = await _reorder(client, token, c1, None)
    assert resp.status_code == 200, resp.text

    refs = await _list_refs(client, token, f"document_id={c}")
    order = [r["reference_id"] for r in refs]
    # Newest-first creation order inside the untouched tiers: p2 (created after
    # p1) holds the smaller key; c1 was dragged to the top of its tier above.
    assert order == [c1, c2, p2, p1, i1], order
    tiers = [[c1, c2], [p2, p1], [i1]]
    pos = 0
    for tier in tiers:
        seg = refs[pos:pos + len(tier)]
        keys = [r["sort_key"] for r in seg]
        assert keys == sorted(keys) and len(set(keys)) == len(keys), keys
        pos += len(tier)

    # A content edit bumps updated_at but must NOT move the ref.
    resp = await client.patch(
        f"/api/references/{c2}", json={"content": "edited body"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    after = [r["reference_id"] for r in await _list_refs(client, token, f"document_id={c}")]
    assert after == order, after


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


# ─── Reorder refusals: cross-group after_id, archived on either side ─────────


@pytest.mark.asyncio
async def test_ref_reorder_refusals(client, admin_user, project_with_doc, test_db):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    p = await _mk_doc(client, token, pid, "P", parent_id=idx_id)
    c = await _mk_doc(client, token, pid, "C", parent_id=p)
    parent_group_ref = await _mk_ref(client, token, pid, p, "pr")
    cr1 = await _mk_ref(client, token, pid, c, "cr1")
    cr2 = await _mk_ref(client, token, pid, c, "cr2")
    child_doc = await _mk_doc(client, token, pid, "CC", parent_id=c)
    archived = await _mk_ref(client, token, pid, c, "arch")
    resp = await client.patch(
        f"/api/references/{archived}", json={"archived": True},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text

    for bad_after in (parent_group_ref, child_doc, archived):
        resp = await _reorder(client, token, cr1, bad_after)
        assert resp.status_code == 400, (bad_after, resp.text)
        assert (await _row(test_db, cr1))["parent"] == c

    # An archived ref is not hand-sortable at all.
    resp = await _reorder(client, token, archived, None)
    assert resp.status_code == 400, resp.text
    resp = await _reorder(client, token, archived, cr1)
    assert resp.status_code == 400, resp.text
    assert (await _row(test_db, cr2))["parent"] == c


# ─── Access: same gate as every ref write ────────────────────────────────────


@pytest.mark.asyncio
async def test_ref_reorder_access_levels(
    client, admin_user, regular_user, project_with_doc, test_db,
):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    uid, utoken = regular_user
    d = await _mk_doc(client, token, pid, "D", parent_id=idx_id)
    r1 = await _mk_ref(client, token, pid, d, "r1")
    await _mk_ref(client, token, pid, d, "r2")

    for level in ("commentator", "readonly"):
        await _add_member(uid, pid, level)
        resp = await client.patch(
            f"/api/documents/{r1}/reorder", json={"after_id": None},
            cookies={"lore_session": utoken},
        )
        assert resp.status_code == 403, (level, resp.text)
        await test_db.query(
            "DELETE project_members WHERE project_id = $pid AND user_id = $uid",
            {"pid": pid, "uid": uid},
        )

    await _add_member(uid, pid, "full")
    resp = await client.patch(
        f"/api/documents/{r1}/reorder", json={"after_id": None},
        cookies={"lore_session": utoken},
    )
    assert resp.status_code == 200, resp.text


# ─── Event: the existing document_reordered, on the ref id ───────────────────


@pytest.mark.asyncio
async def test_ref_reorder_emits_document_reordered(client, admin_user, project_with_doc, test_db):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    d = await _mk_doc(client, token, pid, "D", parent_id=idx_id)
    r1 = await _mk_ref(client, token, pid, d, "r1")
    r2 = await _mk_ref(client, token, pid, d, "r2")

    with EmitRecorder.active() as rec:
        resp = await _reorder(client, token, r2, None)
    assert resp.status_code == 200, resp.text
    events = rec.of("document_reordered")
    assert events, rec.names()
    payload = events[0]
    assert payload["document_id"] == r2
    assert payload["parent_id"] == d
    new_key = (await _row(test_db, r2))["sort_key"]
    assert payload["sort_key"] == new_key
    assert new_key < (await _row(test_db, r1))["sort_key"]


# ─── Re-host: top key of the new host's ref group, events carry it ───────────


@pytest.mark.asyncio
async def test_rehost_via_patch_and_tree_move(
    client, admin_user, project_with_doc, test_db,
):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    p = await _mk_doc(client, token, pid, "P", parent_id=idx_id)
    d = await _mk_doc(client, token, pid, "D", parent_id=idx_id)
    p_existing = await _mk_ref(client, token, pid, p, "p-existing")
    r = await _mk_ref(client, token, pid, d, "r")

    # reference_moved fires through the REAL bus (references.py holds a
    # top-level `from event_bus import emit` binding, which EmitRecorder does
    # not reach — only call-time resolutions are recordable).
    import asyncio

    from event_bus import off as bus_off
    from event_bus import on as bus_on
    moved_events: list[dict] = []

    async def _on_reference_moved(**kwargs):
        moved_events.append(kwargs)

    bus_on("reference_moved", _on_reference_moved)
    try:
        resp = await client.patch(
            f"/api/references/{r}", json={"document_id": p},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200, resp.text
        await asyncio.sleep(0)
    finally:
        bus_off("reference_moved", _on_reference_moved)
    row = await _row(test_db, r)
    assert row["parent"] == p
    assert isinstance(row["sort_key"], str) and row["sort_key"]
    assert row["sort_key"] < (await _row(test_db, p_existing))["sort_key"]
    assert moved_events, "reference_moved must fire on re-host"
    assert moved_events[0]["document_id"] == p
    assert moved_events[0]["sort_key"] == row["sort_key"]

    # Tree move (PATCH /api/documents parent_id) re-hosts through the same writer.
    r2 = await _mk_ref(client, token, pid, d, "r2")
    with EmitRecorder.active() as rec2:
        resp = await client.patch(
            f"/api/documents/{r2}", json={"parent_id": p},
            cookies={"lore_session": token},
        )
    assert resp.status_code == 200, resp.text
    row2 = await _row(test_db, r2)
    assert row2["parent"] == p
    assert isinstance(row2["sort_key"], str) and row2["sort_key"]
    assert row2["sort_key"] < row["sort_key"], "must land at the TOP of P's ref group"
    dm = rec2.of("document_moved")
    assert dm, rec2.names()
    assert dm[0]["sort_key"] == row2["sort_key"]


# ─── Project-scope LIST: archived last, groups contiguous, key order ─────────


@pytest.mark.asyncio
async def test_project_scope_list_groups_contiguous_archived_last(
    client, admin_user, project_with_doc,
):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    p = await _mk_doc(client, token, pid, "P", parent_id=idx_id)
    d = await _mk_doc(client, token, pid, "D", parent_id=idx_id)
    await _mk_ref(client, token, pid, idx_id, "i1")
    await _mk_ref(client, token, pid, p, "p1")
    await _mk_ref(client, token, pid, p, "p2")
    await _mk_ref(client, token, pid, d, "d1")
    await _mk_ref(client, token, pid, d, "d2")
    d_arch = await _mk_ref(client, token, pid, d, "d-arch")
    resp = await client.patch(
        f"/api/references/{d_arch}", json={"archived": True},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text

    refs = await _list_refs(client, token, f"project_id={pid}")
    order = [r["reference_id"] for r in refs]
    assert order[-1] == d_arch, order
    live = [r for r in refs if r["reference_id"] != d_arch]
    by_parent = {}
    for r in live:
        by_parent.setdefault(r["document_id"], []).append(r)
    for parent, group in by_parent.items():
        assert len(group) >= 1
        keys = [r["sort_key"] for r in group]
        assert keys == sorted(keys), (parent, keys)
    # Contiguity: each parent's refs form one run in the served order.
    seen_parents = [r["document_id"] for r in live]
    for parent in by_parent:
        runs = [i for i, x in enumerate(seen_parents) if x == parent]
        assert runs == list(range(runs[0], runs[0] + len(runs))), (
            parent, seen_parents,
        )


# ─── Mixed readers: refs keep the band they occupy today (RED-first pin) ─────


@pytest.mark.asyncio
@pytest.mark.parametrize("ref_key", [None, "a0"])
async def test_config_loader_keeps_refs_in_their_band(
    client, test_db, project_with_doc, ref_key,
):
    """agent_config_load's BFS: docs and refs of one config folder keep TODAY's
    relative band even once the ref carries a key. MEASURED (RED-first run
    against unchanged code): SurrealDB ORDER BY sort_key ASC places NONE BEFORE
    strings, so refs LEAD docs in the config walk today — the pin is refs-first
    here (structure_exec sorts in Python and trails them; each reader keeps its
    own measured band)."""
    pid, idx_id, _ = project_with_doc
    from agent_config import ensure_agent_system_docs
    from agent_config_load import _bfs_config_subtree

    ensured = await ensure_agent_system_docs(pid)
    rules = ensured["rules_folder"]

    async def mk(did, title, sk, *, is_ref=False):
        row = {
            "project_id": pid, "parent_id": rules, "title": title,
            "content": "x", "path": f".lore/system/x/{did}", "is_index": False,
        }
        if sk is not None:
            row["sort_key"] = sk
        if is_ref:
            row.update(is_reference=True, media_type="markdown",
                       path=f"_ref/{did}.md")
        await create_record("documents", did, row)

    await mk("ord-doc-c", "Doc C", "c")
    await mk("ord-doc-k", "Doc K", "k")
    await mk("ord-ref", "Ref", ref_key, is_ref=True)

    children = await _bfs_config_subtree(test_db, pid, None, [rules])
    rows = next(iter(children.values()))
    ids = [r["id"] for r in rows]
    docs = [i for i in ids if i.startswith("ord-doc")]
    ref_pos = ids.index("ord-ref")
    assert docs == ["ord-doc-c", "ord-doc-k"], ids
    assert ref_pos == 0, (
        f"refs must keep today's leading band in the config walk: {ids}"
    )


def test_structure_sibling_order_keeps_refs_in_their_band():
    """structure_exec._sibling_order: a keyed ref and a legacy NONE ref keep the
    trailing band docs occupy today — keys must not interleave refs with docs."""
    from agent.structure_exec import _sibling_order

    topo = {
        "doc-k": {"is_index": False, "sort_key": "k"},
        "doc-c": {"is_index": False, "sort_key": "c"},
        "ref-a0": {"is_index": False, "is_reference": True, "sort_key": "a0"},
        "ref-none": {"is_index": False, "is_reference": True, "sort_key": None},
    }
    order = _sibling_order(topo, list(topo))
    assert order == ["doc-c", "doc-k", "ref-a0", "ref-none"], order


# ─── Key space includes archived refs ────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_ref_in_archived_only_group_gets_distinct_key(
    client, admin_user, project_with_doc, test_db,
):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    d = await _mk_doc(client, token, pid, "D", parent_id=idx_id)
    arch1 = await _mk_ref(client, token, pid, d, "arch1")
    arch2 = await _mk_ref(client, token, pid, d, "arch2")
    for rid in (arch1, arch2):
        resp = await client.patch(
            f"/api/references/{rid}", json={"archived": True},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200, resp.text

    r = await _mk_ref(client, token, pid, d, "new-live")
    new_key = (await _row(test_db, r))["sort_key"]
    assert isinstance(new_key, str) and new_key
    for rid in (arch1, arch2):
        assert new_key != (await _row(test_db, rid))["sort_key"]


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
