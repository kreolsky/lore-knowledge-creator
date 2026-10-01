"""Pydantic models: references domain (split out of the former models.py).

# ARCH: these typed models are the request/response boundary of the /api/references
# facade — a PERMANENT public-API abstraction, not a compat shim. Internally
# references are stored as `documents` rows with is_reference=true — that folding
# fact has ONE wording source, and this module docstring is it: refs and docs share
# the documents table, the doc_chunks store and the retrieval path, and the legacy
# "ref" entity_type is treated as "doc" wherever chunks are keyed by document_id.
# (retrieval.py and embeddings.py point here instead of restating it.) The
# /api/references surface deliberately hides that STI storage choice so clients
# speak reference_id / document_id and never learn that "a reference is a document
# with a flag" (see the mirror reasoning in routes/references.py). Do NOT collapse
# this router/models pair into /api/documents.
"""
from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    field_validator,
)


# ─── Reference request/response models ───────────────────────────────────────
class CreateReference(BaseModel):
    project_id: str
    document_id: str | None = None
    title: str
    media_type: Literal["markdown", "audio", "image"] = "markdown"
    source_url: str | None = None
    content: str | None = None


class PatchReference(BaseModel):
    content: str | None = None
    title: str | None = None
    document_id: str | None = None
    # staged-delete stage 1. `archived=true` hides the ref
    # from the default LIST (restorable); the second click on the trash soft-deletes it.
    archived: bool | None = None


class DeleteReferencesRequest(BaseModel):
    reference_ids: list[str] = Field(min_length=1, max_length=100)


class WidgetSession(BaseModel):
    """External session identity on a widget-extract upload (the `session` form
    field of POST /api/widget/extract, a JSON string).

    The `id` is the clinic-side appointment/session id: it becomes the
    idempotency key component, so a replay of the same id returns the SAME job
    instead of a duplicate row (the collision class of 883 docs / 867 values in
    cir/docs/00-handoff.md). `protocol` is reserved for T4 (protocol → config
    selection): accepted and stored, never read.
    """

    id: str
    protocol: str | None = None

    @field_validator("id")
    @classmethod
    def _id_stripped_nonblank(cls, v: str) -> str:
        # WHY strip-and-reject, not just min_length: a whitespace-only id would
        # mint a distinct idempotency key per invisible padding and silently
        # defeat replay. The stripped form IS the session id (one canonical key).
        v = v.strip()
        if not v:
            raise ValueError("session.id must be a non-empty string")
        return v


# ─── Reference response models (debt-paydown W1 pilot) ───────────────────────
# ARCH: the /api/references LIST route projects an EXPLICIT column set
# (db.REF_META_COLUMNS + has_content via _REF_META_SELECT), so its response field set is
# fixed/deterministic — attaching response_model here has ZERO blast radius (no SELECT *
# to silently filter). The pilot (W1) measured this live: the 18 keys below are exactly
# the unfiltered response keys, so response_model drops nothing.
# The full GET route (_serialize_ref over SELECT *) is deliberately NOT given a model in
# this pilot — its field set is the whole `documents` row, which a static model cannot
# track without drifting with the schema (that class ⇒ option C, not B).
class ReferenceMetaResponse(BaseModel):
    # All fields Optional: the projection fixes the KEY set (so response_model drops
    # nothing), and lenient types mean a null value on any one row (e.g. legacy
    # null project_id from the SCHEMALESS class) cannot ResponseValidationError
    # the whole LIST into a 500.
    reference_id: str | None = None
    project_id: str | None = None
    document_id: str | None = None
    # Persisted manual order within the host's reference group ((sort_key, id)
    # ASC); surfaced so the panel renders the shared order without a re-sort.
    sort_key: str | None = None
    title: str | None = None
    media_type: str | None = None
    source_url: str | None = None
    path: str | None = None
    is_index: bool | None = None
    is_reference: bool | None = None
    processing_status: str | None = None
    file_path: str | None = None
    file_meta: dict | None = None
    # Surfaced through REF_META_COLUMNS + this model so the
    # frontend can dim archived refs in place and render an Archive badge.
    archived: bool | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # Reference author attribution: the creator's identity + denormalized nickname,
    # surfaced so the RefCard meta row can show the nick for someone else's reference.
    # None for impersonal/legacy creations → the UI renders no author segment
    # (no "System" label).
    created_by: str | None = None
    created_by_name: str | None = None
    has_content: bool | None = None
    created_at_fmt: str | None = None
    updated_at_fmt: str | None = None


def is_ref_row(row: dict) -> bool:
    """True only when a documents ROW's `is_reference` is explicitly the boolean True.

    Classification rule: apply this ONLY to documents ROW dicts fetched from the DB
    (rows, projections, batched ref_maps of documents). Do NOT use it on client/agent-
    supplied args or proposal payloads — those carry real bools from Pydantic models,
    so a loose `.get("is_reference")` is correct there and `is_ref_row` would only add
    noise that erodes the call-site contract.

    # WHY: strict identity check (is True), not truthy. Why: a half-typed or
    # legacy documents row may carry 1 / "true" / other non-boolean truthy values — a
    # loose check would misclassify it as a reference. The schema backfill
    # (UPDATE documents SET is_reference = false WHERE is_reference = NONE) keeps live
    # rows boolean, so this is behavior-identical on backfilled data while still
    # rejecting the non-bool shapes the INVARIANT exists to guard. Do NOT simplify to
    # `bool(row.get(...))` or `if row.get(...)`.
    """
    return row.get("is_reference") is True
