"""Inbox REST surface — read/mark-read + per-document notify toggles.

# SYSTEM: inbox — the reader half. The flag itself is written ONLY by
backend/inbox.py::mark_arrived (see the module ARCH there); these routes expose
the pool summary, the open-clears call, and the (user x document) toggles.

Read = OPENED: the frontend calls POST /api/inbox/read when the user opens a
flagged note thread or reference. Clearing is authorized by flag EQUALITY (the
recipient is the only caller whose flag matches), so a non-recipient gets the
same 204 as a no-op — no existence oracle, no access error to distinguish.

GET /api/projects/{project_id}/inbox serves the per-document counts the tree
rows, their ancestors and the notes/refs tabs paint; it is per-CALLER by
construction (the summary queries bind the caller's user id).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from inbox import KIND_NOTE, KIND_REF, doc_notify, mark_read, set_doc_notify, summary
from pydantic import BaseModel
from surrealdb import AsyncSurreal

from access import get_project_access
from auth import get_current_user
from db import get_db

router = APIRouter()

_KINDS = {KIND_NOTE, KIND_REF}


class InboxReadBody(BaseModel):
    kind: str
    id: str


class InboxTogglesBody(BaseModel):
    notes: bool | None = None
    refs: bool | None = None


@router.post("/api/inbox/read", status_code=204)
async def read_inbox_object(
    body: InboxReadBody,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Clear the caller's unread flag on ONE object (no-op when it isn't theirs)."""
    if body.kind not in _KINDS:
        raise HTTPException(status_code=422, detail="kind must be 'note' or 'ref'")
    await mark_read(body.kind, body.id, user["user_id"])
    return Response(status_code=204)


@router.get("/api/projects/{project_id}/inbox")
async def project_inbox_summary(
    project_id: str,
    user: dict = Depends(get_current_user),
):
    """The caller's unread pool: {documents: {<document_id>: {notes, refs}}}."""
    if await get_project_access(project_id, user) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return await summary(project_id, user["user_id"])


@router.get("/api/projects/{project_id}/inbox/toggles/{document_id}")
async def get_inbox_toggles(
    project_id: str,
    document_id: str,
    user: dict = Depends(get_current_user),
):
    """The caller's per-document notify toggles (defaults notes ON, refs OFF)."""
    if await get_project_access(project_id, user) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return await doc_notify(project_id, user["user_id"], document_id)


@router.put("/api/projects/{project_id}/inbox/toggles/{document_id}")
async def put_inbox_toggles(
    project_id: str,
    document_id: str,
    body: InboxTogglesBody,
    user: dict = Depends(get_current_user),
):
    """Upsert the caller's toggles for one document; omitted fields keep their value."""
    if await get_project_access(project_id, user) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return await set_doc_notify(
        project_id, user["user_id"], document_id,
        notes=body.notes, refs=body.refs,
    )


__all__ = ["router"]
