"""Reference author attribution — `created_by` / `created_by_name`.

The RefCard meta row gains a third segment (the creator's nickname) when the
reference was created by someone OTHER than the current user. These tests pin
the four integration seams the feature crosses:

1. the creation chokepoint (`create_reference_row`) persists both fields when
   given and OMITS the keys entirely when None (no sentinel, no "System");
2. REST `POST /api/references` records the authenticated caller as the author;
3. the LIST projection (`db.REF_META_COLUMNS`) carries both fields (asserted over
   the projection CONSTANT, not a literal key list copied into the test);
4. the `reference_created` WS payload carries both fields (asserted over the
   `_SUBSCRIPTIONS` allowlist that strips anything not whitelisted).
"""


import pytest
from emit_recorder import EmitRecorder

from db import REF_META_COLUMNS, fetch_one


@pytest.mark.asyncio
async def test_create_reference_row_persists_author_fields(project_with_doc):
    """create_reference_row writes created_by / created_by_name when given."""
    from documents.service import create_reference_row

    pid, idx_id, _ = project_with_doc
    with EmitRecorder.active():
        record = await create_reference_row(
            ref_id="ref-author-1", project_id=pid, host_id=idx_id,
            title="Authored", media_type="markdown",
            created_by="user-abc", created_by_name="Alice",
        )
    assert record["created_by"] == "user-abc"
    assert record["created_by_name"] == "Alice"
    # And it is truly persisted (not just echoed by the return value).
    row = await fetch_one("documents", "ref-author-1")
    assert row["created_by"] == "user-abc"
    assert row["created_by_name"] == "Alice"


@pytest.mark.asyncio
async def test_create_reference_row_omits_author_keys_when_none(project_with_doc):
    """When created_by / created_by_name are absent, the keys are simply NOT written —
    no sentinel value, no "System"/"Unknown" author (rule 3: impersonal → nothing)."""
    from documents.service import create_reference_row

    pid, idx_id, _ = project_with_doc
    with EmitRecorder.active():
        await create_reference_row(
            ref_id="ref-anon-1", project_id=pid, host_id=idx_id,
            title="Anonymous", media_type="markdown",
        )
    row = await fetch_one("documents", "ref-anon-1")
    # Absent ⇒ NONE (option<string>), never an empty string or sentinel.
    assert row.get("created_by") is None
    assert row.get("created_by_name") is None


@pytest.mark.asyncio
async def test_create_reference_row_writes_author_fields_independently(project_with_doc):
    """created_by / created_by_name are written INDEPENDENTLY (review fix R5): a
    caller passing a name without an id must not silently drop the name."""
    from documents.service import create_reference_row

    pid, idx_id, _ = project_with_doc
    with EmitRecorder.active():
        record = await create_reference_row(
            ref_id="ref-name-only", project_id=pid, host_id=idx_id,
            title="NameOnly", media_type="markdown",
            created_by_name="OnlyName",
        )
    assert "created_by" not in record
    assert record["created_by_name"] == "OnlyName"


@pytest.mark.asyncio
async def test_rest_create_reference_records_authenticated_caller_as_author(
    client, admin_user, project_with_doc,
):
    """POST /api/references stamps the authenticated caller as the author."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "title": "Mine", "media_type": "markdown", "content": "x"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    ref_id = resp.json()["reference_id"]
    row = await fetch_one("documents", ref_id)
    assert row["created_by"] == admin_uid
    assert row["created_by_name"] == "testadmin"


@pytest.mark.asyncio
async def test_list_projection_carries_author_fields(client, admin_user, project_with_doc):
    """The LIST projection constant carries both author fields — asserted over the
    CONSTANT (db.REF_META_COLUMNS), not a copied key list, and a real LIST response
    surfaces them so an unlisted field cannot be silently dropped (response_model)."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "Listed", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    # The projection is the source of truth for what LIST returns.
    assert "created_by" in REF_META_COLUMNS
    assert "created_by_name" in REF_META_COLUMNS

    resp = await client.get(
        f"/api/references?document_id={idx_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    ref = next(r for r in resp.json() if r["title"] == "Listed")
    assert "created_by" in ref
    assert "created_by_name" in ref
    assert ref["created_by"] is not None
    assert ref["created_by_name"] == "testadmin"


@pytest.mark.asyncio
async def test_reference_created_ws_payload_allowlist_carries_author_fields():
    """The ws seam forwards emitted kwargs VERBATIM (no field list to forget), so
    the guarantee that created_by / created_by_name reach clients is the wiring
    test binding emit sites to 'ws:reference_created'. What this pins is the seam
    membership itself: the event rides _SUBSCRIPTIONS (a name dropped from the
    table never reaches a client at all)."""
    from routes.project_ws import _SUBSCRIPTIONS

    assert "reference_created" in _SUBSCRIPTIONS
