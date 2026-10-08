"""Project members domain service — the DB logic behind the members routes.

The queries of routes/projects_members.py live here, following
backend/files_service.py — domain logic in the service,
the route keeps HTTP shape (Depends wiring, status codes, request/response
envelopes, access_changed emits). No SYSTEM marker (internal extraction; the
project-routes entry stays in projects.py). Owner checks and the
owner-invariant 400s stay at the handlers; the moderator scope travels with
the queries it guards, so its 404s are raised here.
"""

from uuid import uuid4

from fastapi import HTTPException
from surrealdb import AsyncSurreal

from db import create_record, extract_id, record_refs, serialize_record


async def group_member_ids(db: AsyncSurreal, moderator_uid: str) -> list[str]:
    """Active member uids of the moderator's group (excluding the moderator).

    The moderator's scoped admin surface: their project list = projects owned
    by {me} ∪ this set (routes/projects.py, routes/projects_members.py).

    Lives here, not in routes.users: a non-route
    module needs it for `require_moderator_scope`, and the import-shape gate
    forbids non-route imports of routes.*.
    """
    rows = await db.query(
        "SELECT VALUE meta::id(id) FROM users WHERE moderator_id = $mid AND deleted_at IS NONE",
        {"mid": moderator_uid},
    )
    return list(rows or [])


async def require_moderator_scope(
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


async def list_admin_members(
    db: AsyncSurreal, project_id: str, owner_id: str | None,
) -> dict[str, str]:
    """All member rows of a project as {user_id: access_level}.

    # INVARIANT(security): owner is never listed; Why: owner access is implied
    # by owner_id, not a membership row — a stray row would surface the owner
    # as a grantable member.
    """
    rows = await db.query(
        "SELECT user_id, access_level FROM project_members WHERE project_id = $pid",
        {"pid": project_id},
    )
    return {
        r["user_id"]: r["access_level"]
        for r in (rows or [])
        if r["user_id"] != owner_id
    }


async def upsert_member(
    db: AsyncSurreal, *, project_id: str, user_id: str, access_level: str,
) -> None:
    """Set a user's access level, updating the row when one exists."""
    existing = await db.query(
        "SELECT id FROM project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": project_id, "uid": user_id},
    )
    if existing:
        pm_id = extract_id(existing[0]["id"])
        await db.query(
            "UPDATE type::record('project_members', $id) SET access_level = $al",
            {"id": pm_id, "al": access_level},
        )
    else:
        pm_id = str(uuid4())
        await create_record("project_members", pm_id, {
            "project_id": project_id,
            "user_id": user_id,
            "access_level": access_level,
        })


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


async def apply_owner_transfer(
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


async def delete_member(db: AsyncSurreal, *, project_id: str, user_id: str) -> None:
    """Drop a user's explicit membership row (no-op when none exists)."""
    await db.query(
        "DELETE project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": project_id, "uid": user_id},
    )


async def list_members_full(
    db: AsyncSurreal, *, project_id: str, owner_id: str | None,
) -> list[dict]:
    """Real + pending members assembled for the owner's members list.

    # INVARIANT(security): owner is never listed; Why: owner access is implied
    # by owner_id, not a membership row (defense in depth against
    # legacy/regression data).
    """
    rows = await db.query(
        "SELECT user_id, access_level FROM project_members WHERE project_id = $pid",
        {"pid": project_id},
    )
    rows = [r for r in (rows or []) if r["user_id"] != owner_id]
    members: list[dict] = []
    uids = [extract_id(r["user_id"]) for r in (rows or [])]
    user_map: dict[str, dict] = {}
    if uids:
        refs, params = record_refs("users", uids)
        user_rows = await db.query(
            f"SELECT id, name, email FROM users WHERE id IN [{refs}]", params
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
    return members


async def set_member_level(
    db: AsyncSurreal, *, project_id: str, user_id: str, access_level: str,
) -> bool:
    """Update an existing member's level; False (no write) when not a member."""
    existing = await db.query(
        "SELECT id FROM project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": project_id, "uid": user_id},
    )
    if not existing:
        return False
    pm_id = extract_id(existing[0]["id"])
    await db.query(
        "UPDATE type::record('project_members', $id) SET access_level = $al",
        {"id": pm_id, "al": access_level},
    )
    return True
