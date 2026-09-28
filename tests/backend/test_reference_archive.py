"""Reference staged-delete (archive → soft-delete) — plan reference-archive-v2.

Covers step 3: PATCH archived sets the flag + emits exactly one reference_updated;
LIST excludes archived by default (both doc-scope and project-scope); include_archived
includes them AND archived sink to the bottom in BOTH branches (regression guard for the
sort bug); stage-2 DELETE of an archived ref still soft-deletes.
"""
import asyncio

import pytest


async def _make_ref(client, token, pid, doc_id, title, content="body"):
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": doc_id, "title": title, "content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["reference_id"]


@pytest.mark.asyncio
async def test_patch_archived_true_sets_flag_and_emits_reference_updated(
    client, admin_user, project_with_doc,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _make_ref(client, token, pid, doc_id, "ArchMe")

    from event_bus import off as bus_off
    from event_bus import on as bus_on
    events: list[dict] = []

    async def listener(**kwargs):
        events.append(kwargs)

    bus_on("reference_updated", listener)
    try:
        resp = await client.patch(
            f"/api/references/{ref_id}",
            json={"archived": True},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200, resp.text
        await asyncio.sleep(0)
        # Exactly one reference_updated emit (the archive branch — not reference_renamed/
        # reference_moved/content_flushed, which are different event types).
        assert len(events) == 1
        assert events[0]["reference_id"] == ref_id
    finally:
        bus_off("reference_updated", listener)

    single = (await client.get(f"/api/references/{ref_id}", cookies={"lore_session": token})).json()
    assert single["archived"] is True


@pytest.mark.asyncio
async def test_patch_archived_false_restores(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _make_ref(client, token, pid, doc_id, "RestoreMe")

    await client.patch(f"/api/references/{ref_id}", json={"archived": True},
                       cookies={"lore_session": token})
    resp = await client.patch(
        f"/api/references/{ref_id}", json={"archived": False},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    single = (await client.get(f"/api/references/{ref_id}", cookies={"lore_session": token})).json()
    assert single["archived"] is False


@pytest.mark.asyncio
async def test_list_doc_scope_excludes_archived_by_default(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    live = await _make_ref(client, token, pid, doc_id, "Live")
    archived = await _make_ref(client, token, pid, doc_id, "Arch")
    await client.patch(f"/api/references/{archived}", json={"archived": True},
                       cookies={"lore_session": token})

    listed = (await client.get(
        f"/api/references?document_id={doc_id}", cookies={"lore_session": token})).json()
    ids = {r["reference_id"] for r in listed}
    assert live in ids
    assert archived not in ids


@pytest.mark.asyncio
async def test_list_doc_scope_include_archived_sinks_archived_last(
    client, admin_user, project_with_doc,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    live = await _make_ref(client, token, pid, doc_id, "Live")
    # Create the archived ref AFTER live and archive it — its updated_at is newer, so
    # without the archived sink it would sort FIRST (newest). The sink must demote it.
    archived = await _make_ref(client, token, pid, doc_id, "Arch")
    await client.patch(f"/api/references/{archived}", json={"archived": True},
                       cookies={"lore_session": token})

    listed = (await client.get(
        f"/api/references?document_id={doc_id}&include_archived=true",
        cookies={"lore_session": token})).json()
    ids = [r["reference_id"] for r in listed]
    assert set(ids) == {live, archived}
    # Archived sinks to the bottom (regression guard for the doc-scope Python re-sort).
    assert ids[-1] == archived


@pytest.mark.asyncio
async def test_list_project_scope_excludes_archived_by_default(
    client, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, token = admin_user
    live = await _make_ref(client, token, pid, None, "Live")
    archived = await _make_ref(client, token, pid, None, "Arch")
    await client.patch(f"/api/references/{archived}", json={"archived": True},
                       cookies={"lore_session": token})

    listed = (await client.get(
        f"/api/references?project_id={pid}", cookies={"lore_session": token})).json()
    ids = {r["reference_id"] for r in listed}
    assert live in ids
    assert archived not in ids


@pytest.mark.asyncio
async def test_list_project_scope_include_archived_sinks_archived_last(
    client, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, token = admin_user
    live = await _make_ref(client, token, pid, None, "Live")
    archived = await _make_ref(client, token, pid, None, "Arch")
    await client.patch(f"/api/references/{archived}", json={"archived": True},
                       cookies={"lore_session": token})

    listed = (await client.get(
        f"/api/references?project_id={pid}&include_archived=true",
        cookies={"lore_session": token})).json()
    ids = [r["reference_id"] for r in listed]
    assert set(ids) == {live, archived}
    # Archived sinks to the bottom (project-scope SQL ORDER BY archived ASC).
    assert ids[-1] == archived


@pytest.mark.asyncio
async def test_patch_whitespace_title_400s(client, admin_user, project_with_doc):
    """Delta (rename-core-one-path): a whitespace-only reference title PATCH is
    400 "title is required" — today it is written as the title verbatim."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _make_ref(client, token, pid, doc_id, "Keep Me")
    resp = await client.patch(
        f"/api/references/{ref_id}", json={"title": "   "},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "title is required"


@pytest.mark.asyncio
async def test_patch_same_title_emits_nothing(client, admin_user, project_with_doc):
    """Delta (rename-core-one-path): a same-title reference PATCH emits no
    reference_renamed / content_flushed (the rename core's no-op exit)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _make_ref(client, token, pid, doc_id, "Same Ref")

    from event_bus import off as bus_off
    from event_bus import on as bus_on
    events: list[dict] = []

    async def listener(**kwargs):
        events.append(kwargs)

    bus_on("reference_renamed", listener)
    try:
        resp = await client.patch(
            f"/api/references/{ref_id}", json={"title": "Same Ref"},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200, resp.text
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert events == []
    finally:
        bus_off("reference_renamed", listener)


@pytest.mark.asyncio
async def test_stage2_delete_of_archived_ref_soft_deletes(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _make_ref(client, token, pid, doc_id, "ThenDelete")
    await client.patch(f"/api/references/{ref_id}", json={"archived": True},
                       cookies={"lore_session": token})

    resp = await client.delete(f"/api/references/{ref_id}", cookies={"lore_session": token})
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    # Gone even with include_archived (soft-deleted, not just archived).
    listed = (await client.get(
        f"/api/references?document_id={doc_id}&include_archived=true",
        cookies={"lore_session": token})).json()
    assert ref_id not in {r["reference_id"] for r in listed}
