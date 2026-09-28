"""Pydantic models: documents domain (split out of the former models.py)."""
from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    Field,
)


class CreateDocument(BaseModel):
    project_id: str
    title: str | None = Field(default=None, min_length=1, max_length=256)
    path: str | None = None
    parent_id: str | None = None
    content: str | None = Field(default=None, max_length=500_000)
    # WHY: is_reference/media_type are immutable post-creation. PatchDocument
    # intentionally does not expose them — flipping a regular document into a
    # reference would break every link / tree assumption in the codebase.  Why: a document's kind (doc vs reference) is baked into tree/link/retrieval assumptions everywhere; flipping it post-creation would silently corrupt those, so PatchDocument never exposes it.
    is_reference: bool = False
    media_type: Literal["markdown", "audio", "image"] | None = None
    source_url: str | None = None


class PatchDocument(BaseModel):
    content: str | None = None
    title: str | None = None
    parent_id: str | None = None


class ReorderDocument(BaseModel):
    # after_id: the sibling to place this document after; None = top of the group.
    # Server computes the fractional sort_key — never sent by the client (thin-client).
    after_id: str | None = None


class MoveDocumentToProject(BaseModel):
    """Cross-project subtree move (POST /api/documents/{id}/move).

    parent_id resolves INSIDE target_project_id (None = target project root);
    the command validates it with assert_parent_valid against the TARGET.
    """
    target_project_id: str
    parent_id: str | None = None


class DeleteDocumentsRequest(BaseModel):
    document_ids: list[str] = Field(min_length=1, max_length=100)
    # ARCH: delete_children defaults to True — deleting documents deletes their
    # live subtrees with them (parity with the single-delete endpoint). The
    # request-body cap (100 ids) bounds the request; descendant expansion is
    # deliberately uncapped (same shape as project-level delete).
    delete_children: bool = True


class DocumentBatchRequest(BaseModel):
    # ARCH: unified content batch — one projected SELECT resolves N transcluded
    # doc/reference ids for a single project-read check.
    # Cap bounds payload size.
    ids: list[str] = Field(max_length=200)


class CreateCheckpoint(BaseModel):
    document_id: str
    content: str | None = None
    tables_json: str | None = None
    label: str | None = None
    comment: str | None = None


class PatchCheckpoint(BaseModel):
    label: str | None = None
    comment: str | None = None
