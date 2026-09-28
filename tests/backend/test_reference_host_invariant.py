"""Reference host invariant — DB-level guarantee that a reference always has a real host.

Defect being fixed: a "project level" reference had THREE encodings — `parent_id =
index_doc_id` (the one visible encoding) plus `parent_id = NONE/NULL` and `parent_id =
''` (two invisible ones). The invisible forms matched NO document's panel listing (the
`parent_id IN $ids` scan hits none of NONE/NULL/''), so the reference silently vanished.
This suite pins the fix: a DB event makes a no-host reference impossible for ANY writer,
and the two REST writers that mean "project level" normalize `'' / None → index_doc_id`.

Asserts over the SERVED listing wherever the defect was invisibility (the served
panel is the user-facing surface).
"""

from uuid import uuid4

import pytest

from db import create_record, get_db

# ─── DB event rejects ALL three no-host forms ────────────────────────────────


@pytest.mark.asyncio
async def test_event_rejects_reference_with_none_parent(test_db, project_with_doc):
    """is_reference=true + parent_id=None (the IS NONE form, what Python None serializes
    to) must abort the CREATE."""
    pid, _, _ = project_with_doc
    ref_id = str(uuid4())
    try:
        await create_record("documents", ref_id, {
            "project_id": pid,
            "is_reference": True,
            "media_type": "markdown",
            "parent_id": None,
            "title": "NoHostNone",
            "content": "",
            "path": f"_ref/{ref_id}.md",
        })
        raised = False
    except RuntimeError:
        raised = True
    db = await get_db()
    persisted = await db.query(
        "SELECT VALUE meta::id(id) FROM type::record('documents', $id)", {"id": ref_id},
    )
    assert raised or not persisted, "event must abort a no-host (NONE) reference"


@pytest.mark.asyncio
async def test_event_rejects_reference_with_empty_string_parent(test_db, project_with_doc):
    """is_reference=true + parent_id='' (the picker sentinel form) must abort the CREATE."""
    pid, _, _ = project_with_doc
    ref_id = str(uuid4())
    try:
        await create_record("documents", ref_id, {
            "project_id": pid,
            "is_reference": True,
            "media_type": "markdown",
            "parent_id": "",
            "title": "NoHostEmpty",
            "content": "",
            "path": f"_ref/{ref_id}.md",
        })
        raised = False
    except RuntimeError:
        raised = True
    db = await get_db()
    persisted = await db.query(
        "SELECT VALUE meta::id(id) FROM type::record('documents', $id)", {"id": ref_id},
    )
    assert raised or not persisted, "event must abort a no-host ('') reference"


@pytest.mark.asyncio
async def test_event_rejects_reference_with_raw_null_parent(test_db, project_with_doc):
    """A raw SET parent_id = NULL insert (the IS NULL form) must abort.

    create_record sends Python None → NONE, so the NULL form is reached only via a raw
    query. The event's three-form condition covers it explicitly (NONE/NULL/'')."""
    pid, _, _ = project_with_doc
    db = await get_db()
    ref_id = str(uuid4())
    try:
        await db.query(
            "CREATE type::record('documents', $id) CONTENT { "
            "project_id: $pid, is_reference: true, media_type: 'markdown', "
            "parent_id: NULL, title: 'NoHostNull', content: '', path: 'n.md' }",
            {"id": ref_id, "pid": pid},
        )
        raised = False
    except Exception:
        raised = True
    persisted = await db.query(
        "SELECT VALUE meta::id(id) FROM type::record('documents', $id)", {"id": ref_id},
    )
    assert raised or not persisted, "event must abort a no-host (NULL) reference"


@pytest.mark.asyncio
async def test_event_allows_reference_with_real_host(test_db, project_with_doc):
    """A reference with a real host (parent_id = a non-reference doc) is allowed — the
    event must not over-block the legitimate case."""
    pid, idx_id, _ = project_with_doc
    ref_id = str(uuid4())
    record = await create_record("documents", ref_id, {
        "project_id": pid,
        "is_reference": True,
        "media_type": "markdown",
        "parent_id": idx_id,
        "title": "HostedRef",
        "content": "",
        "path": f"_ref/{ref_id}.md",
    })
    assert record["parent_id"] == idx_id


# ─── POST / PATCH normalize '' / None → index_doc_id (project-level intent) ───


@pytest.mark.asyncio
async def test_post_reference_empty_document_id_resolves_to_index_doc(
    client, admin_user, project_with_doc,
):
    """POST with document_id='' (or omitted) re-hosts onto index_doc_id, so the ref is
    VISIBLE from an unrelated document's panel — not silently invisible."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    # An unrelated root document (its ancestor walk is [self], never reaching idx_id).
    sibling = await client.post(
        "/api/documents", json={"project_id": pid, "title": "Sibling"}, cookies=cookies,
    )
    assert sibling.status_code == 200
    sibling_id = sibling.json()["document_id"]

    # Project-level create (the picker's '' sentinel).
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": "", "title": "ProjectRef",
              "media_type": "markdown"},
        cookies=cookies,
    )
    assert resp.status_code == 200
    ref_id = resp.json()["reference_id"]

    # The defect: before the fix this ref was invisible from sibling's panel.
    listing = await client.get(
        f"/api/references?document_id={sibling_id}", cookies=cookies,
    )
    ids = [r["reference_id"] for r in listing.json()]
    assert ref_id in ids, "project-level reference must be visible from every document"


@pytest.mark.asyncio
async def test_post_reference_omitted_document_id_resolves_to_index_doc(
    client, admin_user, project_with_doc,
):
    """POST with document_id OMITTED (None) re-hosts onto index_doc_id too — the
    default-project-level intent, not a silent no-host write."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    sibling = await client.post(
        "/api/documents", json={"project_id": pid, "title": "Sibling2"}, cookies=cookies,
    )
    sibling_id = sibling.json()["document_id"]
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "title": "ProjectRef2", "media_type": "markdown"},
        cookies=cookies,
    )
    assert resp.status_code == 200
    ref_id = resp.json()["reference_id"]
    listing = await client.get(
        f"/api/references?document_id={sibling_id}", cookies=cookies,
    )
    assert ref_id in [r["reference_id"] for r in listing.json()]


@pytest.mark.asyncio
async def test_patch_reference_empty_document_id_resolves_to_index_doc(
    client, admin_user, project_with_doc,
):
    """PATCH with document_id='' re-hosts onto index_doc_id, visible from an unrelated
    document (moving a ref to project level)."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    # Create the reference hosted on the index doc first.
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "MoveRef",
              "media_type": "markdown"},
        cookies=cookies,
    )
    ref_id = resp.json()["reference_id"]
    # An unrelated sibling document.
    sibling = await client.post(
        "/api/documents", json={"project_id": pid, "title": "ObsSib"}, cookies=cookies,
    )
    sibling_id = sibling.json()["document_id"]
    # Move the reference to project level via the '' sentinel.
    resp = await client.patch(
        f"/api/references/{ref_id}",
        json={"document_id": ""}, cookies=cookies,
    )
    assert resp.status_code == 200
    # It must now be visible from the unrelated sibling.
    listing = await client.get(
        f"/api/references?document_id={sibling_id}", cookies=cookies,
    )
    ids = [r["reference_id"] for r in listing.json()]
    assert ref_id in ids, "project-level (moved) reference must be visible everywhere"


# ─── Broken project: no index_doc_id → loud failure, never silent null ────────


@pytest.mark.asyncio
async def test_project_without_index_doc_project_level_create_raises(
    client, admin_user, project_with_doc, test_db,
):
    """A project missing index_doc_id + a project-level create must FAIL LOUDLY, not
    silently write a no-host reference (which the event would then abort anyway)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    # Break the project: clear index_doc_id (a real error state — it is set once at
    # creation and never mutated, so its absence means a corrupted project).
    await test_db.query(
        "UPDATE type::record('projects', $id) SET index_doc_id = NONE", {"id": pid},
    )
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "title": "Broken", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    assert resp.status_code >= 400, "must reject, not silently write a hostless reference"

