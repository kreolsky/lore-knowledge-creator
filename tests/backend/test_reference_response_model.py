"""W1 pilot — /api/references LIST response_model is zero-blast-radius.

Debt-paydown W1 (.kilo/plans/debt-paydown-2026-07-28.md), option B step 1. The LIST route
projects an explicit column set (REF_META_COLUMNS + has_content), so attaching
response_model=list[ReferenceMetaResponse] must drop NO field. The "before" set was
captured live from the unfiltered route (17 keys, see git history of this file's first
revision); this test asserts the response_model-filtered response carries exactly that set
— proving the pilot has zero blast radius on the LIST route.
"""
import pytest
from test_files import _make_doc

# Captured live from the unfiltered /api/references LIST response (pre-response_model),
# + `archived` (plan reference-archive-v2 step 2 — surfaced via REF_META_COLUMNS + the
# ReferenceMetaResponse model so the frontend can dim archived refs in place).
# + `created_by` / `created_by_name` (plan reference-card-author-nickname — the
# reference creator's identity + denormalized nick for the RefCard meta row).
# + `sort_key` (the persisted manual order within the host's ref group).
# + `unread` (the VIEWER-relative inbox flag;
# the raw unread_for column is popped at the serializer and never surfaces).
EXPECTED_LIST_KEYS = {
    "archived", "created_at", "created_at_fmt", "document_id", "file_meta", "file_path",
    "has_content", "is_index", "is_reference", "media_type", "path",
    "processing_status", "project_id", "reference_id", "sort_key", "source_url", "title",
    "updated_at", "updated_at_fmt",
    "created_by", "created_by_name",
    "unread",
}


@pytest.mark.asyncio
async def test_reference_list_response_model_no_field_loss(client, admin_user, project_with_doc):
    from models import ReferenceMetaResponse

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "RefPilotDoc")

    create = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": doc_id, "title": "Pilot Ref", "content": "body"},
        cookies={"lore_session": token},
    )
    assert create.status_code == 200, create.text

    listed = await client.get(
        f"/api/references?document_id={doc_id}",
        cookies={"lore_session": token},
    )
    # No 500 ⇒ response_model validation passed against the real row values.
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert isinstance(body, list) and body

    actual_keys = set(body[0].keys())
    declared_keys = set(ReferenceMetaResponse.model_fields.keys())
    # Zero blast radius: response_model dropped nothing (response == unfiltered before),
    # and the model declares exactly the projection (no extra/missing field).
    assert actual_keys == EXPECTED_LIST_KEYS, (
        f"response dropped/added keys vs unfiltered: "
        f"missing={EXPECTED_LIST_KEYS - actual_keys} extra={actual_keys - EXPECTED_LIST_KEYS}"
    )
    assert declared_keys == EXPECTED_LIST_KEYS, (
        f"model drifts from projection: "
        f"missing={EXPECTED_LIST_KEYS - declared_keys} extra={declared_keys - EXPECTED_LIST_KEYS}"
    )


def test_reference_meta_response_accepts_null_fields():
    """W1 review fix: nullable model fields must not 500 the LIST.

    project_id is schema-required (string) for rows written under SCHEMAFULL, but legacy
    SCHEMALESS data (the 2026-07-28 class) can carry nulls. With every field Optional, a
    single row with a null value validates instead of raising ResponseValidationError and
    500-ing the whole list (the regression the review flagged on this hot endpoint).
    """
    from models import ReferenceMetaResponse

    obj = ReferenceMetaResponse(reference_id=None, project_id=None)
    assert obj.reference_id is None and obj.project_id is None
