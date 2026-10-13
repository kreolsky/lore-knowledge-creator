"""Chat search — the person's own chats of a project, for the search panel's "Chats" mode.

The search itself is chat_sessions.recall_search.search_chats, shared with the
agent's `search_my_chats` tool so both run ONE live-row security predicate.

# WHY cookie only: the route's one consumer is the person's own UI; the agent
# already reaches the same corpus as its key's owning user through
# `search_my_chats`, so a Bearer path here would be a second, unconsumed door
# to private chats.
"""
from chat_sessions.recall import chat_recall_disabled
from chat_sessions.recall_search import ChatScope, require_scope_document, search_chats
from fastapi import APIRouter, Depends, HTTPException, Query
from surrealdb import AsyncSurreal

from access import get_project_access
from auth import get_current_user
from db import get_db

# Not the shared chat router: that one carries the /api/chat prefix, and this
# path is project-scoped like semantic-search.
router = APIRouter(tags=["chat"])


@router.get("/api/projects/{project_id}/chat-search")
async def chat_search(
    project_id: str,
    q: str = Query(..., min_length=1, max_length=500),
    scope: ChatScope = Query("project"),
    under_document_id: str | None = Query(None, min_length=1),
    anchor_document_id: str | None = Query(None, min_length=1),
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Semantic search over the caller's own visible chats of the project.

    `anchor_document_id` (the document open in the editor) turns the sibling
    tier on in `project` scope and is the narrow of `siblings`; `subtree` needs
    `under_document_id`. Refused while the admin disabled the `chat-recall`
    skill — the index is not fed then, so results would be silently stale.
    """
    if not await get_project_access(project_id, user):
        raise HTTPException(status_code=403, detail="No access to this project")
    if await chat_recall_disabled(db):
        raise HTTPException(status_code=403,
                            detail="chat-recall is disabled by the administrator")
    if (scope == "subtree") != (under_document_id is not None):
        raise HTTPException(
            status_code=422,
            detail="`under_document_id` is required with scope=subtree, and only there")
    if anchor_document_id:
        await require_scope_document(project_id, anchor_document_id)
    result = await search_chats(
        db, user["user_id"], project_id, q, exclude=set(), scope=scope,
        anchor_doc_id=anchor_document_id, under_document_id=under_document_id)
    return {"query": q, **result}
