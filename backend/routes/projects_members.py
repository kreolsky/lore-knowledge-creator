"""Project members + invites routes — admin member CRUD and the owner-only
self-service invite surface.

Part of the project-routes system: the admin `project_members` endpoints, the
owner-only members list/invite/patch/remove, and pending-invite cancellation
register on the SAME router as projects.py (the sessions_list.py precedent).
CRUD, search, and the document tree stay in `projects.py`. No SYSTEM marker
(internal extraction; the project-routes entry stays in projects.py).
"""
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, HTTPException, Query
from surrealdb import AsyncSurreal

import event_bus
from access import require_project_owner
from auth import get_current_user, require_user_manager_role
from db import (
    create_record,
    extract_id,
    fetch_one,
    get_db,
    serialize_record,
    validate_record_id,
)
from models import (
    CancelInvite,
    InviteMember,
    PatchMember,
    SetProjectMember,
    TransferProjectOwner,
)
from routes.projects import router
from routes.users import group_member_ids


async def _require_moderator_scope(
    db: AsyncSurreal, user: dict, project: dict | None, target_uid: str | None = None,
) -> None:
    """Moderator scope for the admin member routes; a no-op for an admin.

    The scope set is {me} ∪ active group members. The project's owner must be
    in it, and so must the member being written (target_uid) — the moderator
    is a group admin: they may grant themself or their users, never a foreign
    account. Both refusals are 404, never 403.
    # INVARIANT(security): a moderator cannot learn that a foreign project or
    # user exists — the admin UI only offers the scoped user list, this guards
    # the direct API call. Why: same posture as require_user_manager.
    """
    if user.get("role") != "moderator":
        return
    allowed = {user["user_id"], *await group_member_ids(db, user["user_id"])}
    owner_id = project.get("owner_id") if project else None
    if not owner_id or owner_id not in allowed:
        raise HTTPException(status_code=404, detail="Project not found")
    if target_uid is not None and target_uid not in allowed:
        raise HTTPException(status_code=404, detail="User not found")


@router.get("/api/admin/projects/{project_id}/members")
async def list_project_members(
    project_id: str,
    user: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    """Admin: list all members of a project with their access levels.

    Moderator: only when the project's owner is in the moderator's scoped set
    ({me} ∪ group members) — else 404, so a foreign project does not exist to
    them (`_require_moderator_scope`).

    # INVARIANT: owner is never listed; Why: owner access is implied by
    # owner_id, not a membership row.
    """
    project = await fetch_one("projects", project_id)
    await _require_moderator_scope(db, user, project)
    owner_id = project.get("owner_id") if project else None
    rows = await db.query(
        "SELECT user_id, access_level FROM project_members WHERE project_id = $pid",
        {"pid": project_id},
    )
    return {
        r["user_id"]: r["access_level"]
        for r in (rows or [])
        if r["user_id"] != owner_id
    }


@router.post("/api/admin/projects/{project_id}/members")
async def set_project_member(
    project_id: str,
    body: SetProjectMember,
    user: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    """Admin | moderator (scoped, `_require_moderator_scope`): set or update a
    user's access level for a project."""
    project = await fetch_one("projects", project_id)
    await _require_moderator_scope(db, user, project, body.user_id)
    # INVARIANT: owner is never a project_members row; Why: owner access is
    # implied by projects.owner_id. An admin setting the owner's level would
    # be meaningless (ignored by get_project_access) and leak into GET /members.
    if project and body.user_id == project.get("owner_id"):
        raise HTTPException(status_code=400, detail="Owner is not a member")
    existing = await db.query(
        "SELECT id FROM project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": project_id, "uid": body.user_id},
    )
    if existing:
        pm_id = extract_id(existing[0]["id"])
        await db.query(
            "UPDATE type::record('project_members', $id) SET access_level = $al",
            {"id": pm_id, "al": body.access_level},
        )
    else:
        pm_id = str(uuid4())
        await create_record("project_members", pm_id, {
            "project_id": project_id,
            "user_id": body.user_id,
            "access_level": body.access_level,
        })
    await event_bus.emit("access_changed", project_id=project_id, user_id=body.user_id, access=body.access_level)
    return {"success": True}


async def _demote_old_owner(db: AsyncSurreal, *, project_id: str, old_owner_id: str) -> None:
    """Upsert the previous owner as a `full` member."""
    existing = await db.query(
        "SELECT id FROM project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": project_id, "uid": old_owner_id},
    )
    if existing:
        pm_id = extract_id(existing[0]["id"])
        await db.query(
            "UPDATE type::record('project_members', $id) SET access_level = 'full'",
            {"id": pm_id},
        )
    else:
        await create_record("project_members", str(uuid4()), {
            "project_id": project_id,
            "user_id": old_owner_id,
            "access_level": "full",
        })


async def _apply_owner_transfer(
    db: AsyncSurreal, *, project_id: str, old_owner_id: str | None, new_owner_id: str,
) -> None:
    """Crash-safe write order for an ownership transfer. No transaction —
    SurrealDB refuses BEGIN on the shared connection (see ARCH at
    backend/cascade.py) — so the ORDER is the guarantee: every step is
    idempotent, a crash between steps never leaves the project without a
    working owner, and re-running the transfer converges:
    (1) previous owner → full member FIRST (check-then-update — a blind
        create would raise on the idx_pm_unique UNIQUE index); skipped for an
        ownerless project, which the transfer thereby repairs;
    (2) flip owner_id (the single ownership write);
    (3) the new owner's member row is deleted — owner access is implied by
        owner_id, a row would only leak into member listings (the invariant
        pinned in routes/_access_grants.py).
    """
    if old_owner_id:
        await _demote_old_owner(db, project_id=project_id, old_owner_id=old_owner_id)
    await db.query(
        "UPDATE type::record('projects', $id) SET owner_id = $uid",
        {"id": project_id, "uid": new_owner_id},
    )
    await db.query(
        "DELETE project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": project_id, "uid": new_owner_id},
    )


@router.post("/api/admin/projects/{project_id}/owner")
async def transfer_project_owner(
    project_id: str,
    body: TransferProjectOwner,
    user: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    """Admin | moderator (scoped, `_require_moderator_scope`): transfer project
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
    await _require_moderator_scope(db, user, project, body.user_id)
    old_owner_id = project.get("owner_id")
    if body.user_id == old_owner_id:
        raise HTTPException(status_code=400, detail="Already the owner")
    target = await fetch_one("users", body.user_id)
    if not target or target.get("deleted_at"):
        raise HTTPException(status_code=404, detail="User not found")
    await _apply_owner_transfer(
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
    """Admin | moderator (scoped, `_require_moderator_scope`): remove a user's
    explicit access to a project."""
    project = await fetch_one("projects", project_id)
    await _require_moderator_scope(db, user, project, user_id)
    # INVARIANT: owner is never a project_members row; Why: owner access is
    # implied by projects.owner_id. Harmless no-op today (no owner row exists)
    # but pins the invariant cheaply against legacy/regression data.
    if project and user_id == project.get("owner_id"):
        raise HTTPException(status_code=400, detail="Owner is not a member")
    await db.query(
        "DELETE project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": project_id, "uid": user_id},
    )
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
    owner_id = project.get("owner_id")
    rows = await db.query(
        "SELECT user_id, access_level FROM project_members WHERE project_id = $pid",
        {"pid": project_id},
    )
    # INVARIANT: owner is never listed; Why: owner access is implied by owner_id,
    # not a membership row (defense in depth against legacy/regression data).
    rows = [r for r in (rows or []) if r["user_id"] != owner_id]
    members: list[dict] = []
    uids = [extract_id(r["user_id"]) for r in (rows or [])]
    user_map: dict[str, dict] = {}
    if uids:
        placeholders = [f"type::record('users', '{validate_record_id(u)}')" for u in uids]
        user_rows = await db.query(
            f"SELECT id, name, email FROM users WHERE id IN [{', '.join(placeholders)}]"
        )
        for u in (user_rows or []):
            ser = serialize_record(u, "user_id")
            user_map[ser["user_id"]] = ser
    for r in (rows or []):
        uid = extract_id(r["user_id"])
        info = user_map.get(uid, {})
        members.append({
            "user_id": uid,
            "email": info.get("email", ""),
            "name": info.get("name", ""),
            "access_level": r["access_level"],
        })
    pending = await db.query(
        "SELECT email, access_level FROM pending_invites WHERE project_id = $pid",
        {"pid": project_id},
    )
    for p in (pending or []):
        members.append({
            "user_id": "",
            "email": p["email"],
            "name": "",
            "access_level": p["access_level"],
            "pending": True,
        })
    return {"members": members}


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
    existing = await db.query(
        "SELECT id FROM project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": project_id, "uid": user_id},
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Member not found")
    pm_id = extract_id(existing[0]["id"])
    await db.query(
        "UPDATE type::record('project_members', $id) SET access_level = $al",
        {"id": pm_id, "al": body.access_level},
    )
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
    await db.query(
        "DELETE project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": project_id, "uid": user_id},
    )
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
