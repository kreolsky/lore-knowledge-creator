"""Project members + invites routes — admin member CRUD and the owner-only
self-service invite surface.

Part of the project-routes system: the admin `project_members` endpoints, the
owner-only members list/invite/patch/remove, and pending-invite cancellation
register on the SAME router as projects.py (the sessions_list.py precedent).
CRUD, search, and the document tree stay in `projects.py`. No SYSTEM marker
(internal extraction; the project-routes entry stays in projects.py).

The DB logic lives in project_members_service (the files_service
precedent): handlers keep HTTP shape — Depends wiring, status
codes, envelopes, and the access_changed emits (the moderator scope's 404s
travel with its queries).
"""
from typing import Annotated

from fastapi import Depends, HTTPException, Query
from project_members_service import (
    apply_owner_transfer,
    delete_member,
    list_admin_members,
    list_members_full,
    require_moderator_scope,
    set_member_level,
    upsert_member,
)
from surrealdb import AsyncSurreal

import event_bus
from access import require_project_owner
from auth import get_current_user, require_user_manager_role
from db import fetch_one, get_db
from models import (
    CancelInvite,
    InviteMember,
    PatchMember,
    SetProjectMember,
    TransferProjectOwner,
)
from routes.projects import router


@router.get("/api/admin/projects/{project_id}/members")
async def list_project_members(
    project_id: str,
    user: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    """Admin: list all members of a project with their access levels.

    Moderator: only when the project's owner is in the moderator's scoped set
    ({me} ∪ group members) — else 404, so a foreign project does not exist to
    them (`require_moderator_scope`).

    # INVARIANT: owner is never listed; Why: owner access is implied by
    # owner_id, not a membership row.
    """
    project = await fetch_one("projects", project_id)
    await require_moderator_scope(db, user, project)
    owner_id = project.get("owner_id") if project else None
    return await list_admin_members(db, project_id, owner_id)


@router.post("/api/admin/projects/{project_id}/members")
async def set_project_member(
    project_id: str,
    body: SetProjectMember,
    user: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    """Admin | moderator (scoped, `require_moderator_scope`): set or update a
    user's access level for a project."""
    project = await fetch_one("projects", project_id)
    await require_moderator_scope(db, user, project, body.user_id)
    # INVARIANT: owner is never a project_members row; Why: owner access is
    # implied by projects.owner_id. An admin setting the owner's level would
    # be meaningless (ignored by get_project_access) and leak into GET /members.
    if project and body.user_id == project.get("owner_id"):
        raise HTTPException(status_code=400, detail="Owner is not a member")
    await upsert_member(
        db, project_id=project_id, user_id=body.user_id, access_level=body.access_level,
    )
    await event_bus.emit("access_changed", project_id=project_id, user_id=body.user_id, access=body.access_level)
    return {"success": True}


@router.post("/api/admin/projects/{project_id}/owner")
async def transfer_project_owner(
    project_id: str,
    body: TransferProjectOwner,
    user: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    """Admin | moderator (scoped, `require_moderator_scope`): transfer project
    ownership to `body.user_id`; the previous owner becomes a `full` member.
    The target need not already be a member (the admin UI only offers members,
    the API does not require it).

    # INVARIANT(security): this handler is the ONLY write path of
    # projects.owner_id after project creation. Why: owner-only surfaces
    # (access.is_project_root) key on it, so a second writer would be a
    # privilege-escalation path.
    """
    project = await fetch_one("projects", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_moderator_scope(db, user, project, body.user_id)
    old_owner_id = project.get("owner_id")
    if body.user_id == old_owner_id:
        raise HTTPException(status_code=400, detail="Already the owner")
    target = await fetch_one("users", body.user_id)
    if not target or target.get("deleted_at"):
        raise HTTPException(status_code=404, detail="User not found")
    await apply_owner_transfer(
        db, project_id=project_id, old_owner_id=old_owner_id, new_owner_id=body.user_id,
    )
    # The event every member mutation emits: collab WS notify + per-session
    # agent-key revocation — both wanted for an ownership change.
    await event_bus.emit("access_changed", project_id=project_id, user_id=body.user_id, access="full")
    if old_owner_id:
        await event_bus.emit("access_changed", project_id=project_id, user_id=old_owner_id, access="full")
    return {"owner_id": body.user_id, "owner_name": target.get("name")}


@router.delete("/api/admin/projects/{project_id}/members/{user_id}")
async def remove_project_member(
    project_id: str,
    user_id: str,
    user: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    """Admin | moderator (scoped, `require_moderator_scope`): remove a user's
    explicit access to a project."""
    project = await fetch_one("projects", project_id)
    await require_moderator_scope(db, user, project, user_id)
    # INVARIANT: owner is never a project_members row; Why: owner access is
    # implied by projects.owner_id. Harmless no-op today (no owner row exists)
    # but pins the invariant cheaply against legacy/regression data.
    if project and user_id == project.get("owner_id"):
        raise HTTPException(status_code=400, detail="Owner is not a member")
    await delete_member(db, project_id=project_id, user_id=user_id)
    await event_bus.emit("access_changed", project_id=project_id, user_id=user_id, access=None)
    return {"success": True}


# ─── Self-service member CRUD (owner-only, silent invite) ───────────────────


async def _silent_invite(
    *, project_id: str, body: InviteMember, owner_uid: str,
) -> dict:
    """Resolve email → membership write or pending row (project scope).

    Thin wrapper over the shared grant service.

    # WHY: Silent invite — identical response whether the email is registered or queued.
    # Why: prevents enumeration of registered users via the invite endpoint.
    """
    from routes._access_grants import grant_or_queue
    return await grant_or_queue(
        project_id=project_id,
        email=body.email.lower(), access_level=body.access_level,
        invited_by=owner_uid, owner_id=owner_uid,
    )


@router.get("/api/projects/{project_id}/members")
async def get_members(project_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Owner-only: list real + pending members for the project."""
    project = await fetch_one("projects", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_project_owner(project, user)
    return {
        "members": await list_members_full(
            db, project_id=project_id, owner_id=project.get("owner_id"),
        )
    }


@router.post("/api/projects/{project_id}/members")
async def invite_member(
    project_id: str, body: InviteMember, user: dict = Depends(get_current_user),
):
    """Owner-only: invite by email (silent). 200 + {queued: true} regardless of whether the email is registered."""
    project = await fetch_one("projects", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_project_owner(project, user)
    return await _silent_invite(
        project_id=project_id, body=body, owner_uid=user["user_id"],
    )


@router.patch("/api/projects/{project_id}/members/{user_id}")
async def patch_member(
    project_id: str, user_id: str, body: PatchMember,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Owner-only: change a member's role."""
    project = await fetch_one("projects", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_project_owner(project, user)
    if not await set_member_level(
        db, project_id=project_id, user_id=user_id, access_level=body.access_level,
    ):
        raise HTTPException(status_code=404, detail="Member not found")
    await event_bus.emit("access_changed", project_id=project_id, user_id=user_id, access=body.access_level)
    return {"success": True}


@router.delete("/api/projects/{project_id}/members/{user_id}")
async def remove_member(
    project_id: str, user_id: str, user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Owner-only: remove a member. Owner cannot remove themselves (400)."""
    project = await fetch_one("projects", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_project_owner(project, user)
    # INVARIANT: Owner is implicit, not a member row — cannot self-remove.
    # Why: owner role is the manager; self-removal would orphan the project.
    if user_id == project.get("owner_id"):
        raise HTTPException(status_code=400, detail="Owner cannot be removed; transfer ownership instead")
    await delete_member(db, project_id=project_id, user_id=user_id)
    await event_bus.emit("access_changed", project_id=project_id, user_id=user_id, access=None)
    return {"success": True}


@router.delete("/api/projects/{project_id}/invites")
async def cancel_pending_invite(
    project_id: str,
    query: Annotated[CancelInvite, Query()],
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Owner-only: cancel a pending project invite.

    Unknown email is a no-op 200 (no enumeration signal).
    """
    project = await fetch_one("projects", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # WHY: require_project_owner before any DB lookup — no timing/body signal
    # that would let a non-owner enumerate pending invite emails.  Why: gating the owner check before any DB query closes the enumeration side-channel — a non-owner must learn nothing (timing, error shape, body) about pending invites.
    await require_project_owner(project, user)
    from routes._access_grants import _delete_pending_by_scope
    # Shared planner-safe scope delete (same path as the grant upsert) — keeps the
    # cancel and invite-queue paths identical so they cannot diverge.
    await _delete_pending_by_scope(db, query.email.lower(), project_id)
    return {"success": True}
