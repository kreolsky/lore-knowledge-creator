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


import pytest

# ─── POST / PATCH normalize '' / None → index_doc_id (project-level intent) ───


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
