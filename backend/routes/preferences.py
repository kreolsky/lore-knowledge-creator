"""User UI preferences — per-user-per-project persistence.

ARCH: Stores UI layout state (sidebar, panels, tree expansion, last document)
server-side so it syncs across devices.  Global (non-project) prefs use
project_id = '_global' (theme, panel widths).

ARCH: `preferences` is a FLEXIBLE pass-through blob — backend never validates
the shape. Current per-project shape (owned by frontend ui-store.ts):

    {
      sidebarTab: 'docs' | 'toc',
      sidebarOpen: bool,
      collapsedDocIds: [docId, ...],
      lastDocId: str,
      lastDocPosition: { cursor: int, scroll: int, scrollOffset?: int },  # scroll = char offset of the top line, scrollOffset = its px above the edge
      chatModel: str,
      searchQuery: str,
      searchDocuments: bool,
      searchReferences: bool,
      searchMode: 'fulltext' | 'semantic',
      documents: {
        [docId]: {
          mainEntity: { type: 'document'|'reference', id: str },
          rightPanelOpen: bool,
          rightPanelTab: 'notes'|'refs'|'chat'|'checkpoint'|'search'|'outgoing'|'backlinks'|null,
          chatSessionId: str | null,
          selectedReferenceId: str | null,
          refPreviewMode: bool,
        }
      }
    }

Legacy keys (rightPanelTab, rightPanelOpen, chatSessionByDoc, chatSessionByRef,
currentReferenceByDoc, talkToDocuments, refPreviewMode) at top level are ignored
by the client and stripped on next save (clean-slate refactor 2026-05-05;
chatSessionByRef dropped 2026-07-08 — the AI-chat list is now project-wide, so
there is no per-reference restore path). talkToDocuments no longer exists client-
side at all (write-only state removed 2026-07-16) — it stays listed here because
prefs blobs written before that date can still carry it.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from surrealdb import AsyncSurreal

from auth import get_current_user
from db import get_db

router = APIRouter()


class UpsertPreferences(BaseModel):
    preferences: dict


@router.get("/api/preferences/{project_id}")
async def get_preferences(project_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Load UI preferences for the current user in a project."""
    rows = await db.query(
        "SELECT preferences FROM user_preferences "
        "WHERE user_id = $uid AND project_id = $pid LIMIT 1",
        {"uid": user["user_id"], "pid": project_id},
    )
    return rows[0]["preferences"] if rows else {}


@router.put("/api/preferences/{project_id}")
async def save_preferences(
    project_id: str,
    body: UpsertPreferences,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Save (upsert) UI preferences for the current user in a project.

    H-10: Single UPSERT query eliminates the SELECT→CREATE race condition.
    """
    uid = user["user_id"]
    await db.query(
        "UPSERT user_preferences SET user_id = $uid, project_id = $pid, "
        "preferences = $prefs, updated_at = time::now() "
        "WHERE user_id = $uid AND project_id = $pid",
        {"uid": uid, "pid": project_id, "prefs": body.preferences},
    )
    return {"success": True}
