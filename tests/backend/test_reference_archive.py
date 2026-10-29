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
