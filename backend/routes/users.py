"""User management routes — CRUD, facts, project access.

# ARCH: per-user admin actions are gated by auth.require_user_manager (admin →
# any target; moderator → own-group role='user' rows, else 404); the list/create
# routes take the role gate auth.require_user_manager_role (admin | moderator)
# and scope a moderator's rows to moderator_id = <self>.
"""

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from password import hash_secret
from surrealdb import AsyncSurreal

from auth import (
    bump_token_version,
    get_current_user,
    refresh_user_role_cache,
    require_user_manager,
    require_user_manager_role,
)
from db import create_record, fetch_one, get_db, serialize_record, soft_delete
from models import (
    USER_MANAGER_ROLES,
    CreateUserAdmin,
    Role,
    UpdateUser,
    UpdateUserAdmin,
)
from routes._access_grants import _apply_grant

router = APIRouter()

# PATCH field split: manager fields both roles may write; admin fields are
# admin-only (a moderator's body touching them is a 403 — lookup, not a chain).
MANAGER_FIELDS = ("name", "email", "password")
ADMIN_FIELDS = ("role", "moderator_id")


async def _group_size(db: AsyncSurreal, moderator_uid: str) -> int:
    """Active users whose group is this moderator (soft-deleted members don't count)."""
    rows = await db.query(
        "SELECT count() AS n FROM users WHERE moderator_id = $mid AND deleted_at IS NONE GROUP ALL",
        {"mid": moderator_uid},
    )
    return int(rows[0]["n"]) if rows else 0


async def group_member_ids(db: AsyncSurreal, moderator_uid: str) -> list[str]:
    """Active member uids of the moderator's group (excluding the moderator).

    The moderator's scoped admin surface: their project list = projects owned
    by {me} ∪ this set (routes/projects.py, routes/projects_members.py).
    """
    rows = await db.query(
        "SELECT VALUE meta::id(id) FROM users WHERE moderator_id = $mid AND deleted_at IS NONE",
        {"mid": moderator_uid},
    )
    return list(rows or [])


def _group_not_empty(moderator_uid: str, n: int) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": f"Moderator still manages {n} user(s) — reassign them first",
            "group_size": n,
            "moderator_id": moderator_uid,
        },
    )


class _GroupNotEmpty(Exception):
    """Signals a demote/delete of a moderator with active members (→ 409)."""

    def __init__(self, moderator_uid: str, n: int) -> None:
        super().__init__(moderator_uid, n)
        self.moderator_uid = moderator_uid
        self.n = n


async def _collect_admin_field_updates(
    db: AsyncSurreal, target: dict, body: UpdateUserAdmin, current_user: dict, user_id: str,
) -> dict:
    """ADMIN_FIELDS (role, moderator_id) writes under the flat-group transition rules.

    Raises HTTPException (400 self-role-change, 422 illegal group pointer /
    role) or _GroupNotEmpty (demote with active members); returns only the
    admin-field part of the write set. Promotion flattens the group: it clears
    the target's moderator_id unless the same request explicitly sets one —
    that combination is the 422 below, not a silent override.
    """
    role_set = "role" in body.model_fields_set and body.role is not None
    mod_id_set = "moderator_id" in body.model_fields_set
    if role_set and user_id == current_user["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    new_role = body.role if role_set else target.get("role")
    new_mod_id = body.moderator_id if mod_id_set else target.get("moderator_id")
    if target.get("role") == "moderator" and new_role != "moderator":
        n = await _group_size(db, user_id)
        if n:
            raise _GroupNotEmpty(user_id, n)
    if mod_id_set and body.moderator_id is not None:
        mod_row = await fetch_one("users", body.moderator_id)
        if not mod_row or mod_row.get("role") != "moderator":
            raise HTTPException(status_code=422, detail="moderator_id must point at a moderator")
    promotion = role_set and new_role in USER_MANAGER_ROLES
    if promotion and not mod_id_set:
        new_mod_id = None
    if new_mod_id is not None and new_role != "user":
        raise HTTPException(status_code=422, detail="moderator_id is only valid on role='user'")
    updates: dict = {}
    if role_set:
        updates["role"] = body.role
    if mod_id_set or (promotion and target.get("moderator_id") is not None):
        updates["moderator_id"] = new_mod_id
    return updates


def _manager_field_updates(body: UpdateUserAdmin) -> dict:
    """MANAGER_FIELDS (name/email/password) → write set (password → password_hash)."""
    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.email is not None:
        updates["email"] = body.email
    if body.password is not None:
        updates["password_hash"] = hash_secret(body.password)
    return updates


async def _apply_user_updates(db: AsyncSurreal, user_id: str, updates: dict) -> dict:
    """Write the merged field set; returns the serialized-shape row (409 email dup, 404 missing)."""
    if "email" in updates:
        dup = await db.query(
            "SELECT id FROM users WHERE email = $e AND id != type::record('users', $uid) AND deleted_at IS NONE LIMIT 1",
            {"e": updates["email"], "uid": user_id},
        )
        if dup:
            raise HTTPException(status_code=409, detail="Email already in use")
    set_parts = [f"{k} = ${k}" for k in updates]
    set_parts.append("updated_at = time::now()")
    rows = await db.query(
        f"UPDATE type::record('users', $id) SET {', '.join(set_parts)} RETURN AFTER",
        {"id": user_id, **updates},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="User not found")
    # Surreal's RETURN AFTER omits option fields explicitly set to NONE — pin the
    # key so the PATCH response shape is stable for the admin UI.
    row = dict(rows[0])
    row.setdefault("moderator_id", None)
    return row


async def _flush_pending_invites(email: str, new_uid: str) -> None:
    """Apply all queued invites for `email` to the freshly-created user.

    # WHY: Pending invites are applied at user creation; this is the only
    # path that converts a queued grant into real access.
    # Why: silent-invite contract — POST /members never reveals registration state.
    # ARCH: delegates to the shared `_apply_grant` core (no email re-resolution —
    # the uid is already known).
    """
    db = await get_db()
    pending = await db.query(
        "SELECT project_id, access_level FROM pending_invites WHERE email = $e",
        {"e": email.lower()},
    )
    for inv in (pending or []):
        await _apply_grant(
            project_id=inv["project_id"],
            target_uid=new_uid,
            access_level=inv["access_level"],
            db=db,
        )
    await db.query(
        "DELETE pending_invites WHERE email = $e",
        {"e": email.lower()},
    )


@router.get("/api/admin/users")
async def list_users(
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    role: Role | None = Query(default=None),
    manager: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    """List active users — admin sees all (?role= filters for the moderator select);
    a moderator sees only their own group."""
    sql = (
        "SELECT id, name, email, role, user_facts, timezone, created_by, moderator_id, "
        "created_at, token_version, updated_at FROM users WHERE deleted_at IS NONE"
    )
    params: dict = {"limit": limit, "offset": offset}
    if role is not None:
        sql += " AND role = $role"
        params["role"] = role
    if manager.get("role") == "moderator":
        sql += " AND moderator_id = $me"
        params["me"] = manager["user_id"]
    sql += " ORDER BY created_at ASC LIMIT $limit START $offset"
    rows = await db.query(sql, params)
    return await _with_names(db, [serialize_record(r, "user_id") for r in (rows or [])])


async def _with_names(db: AsyncSurreal, users: list[dict]) -> list[dict]:
    """Attach created_by_name / moderator_name to serialized rows — the ONE row
    shape list, create and patch all answer with, so the client splices any
    response into its list (a PATCH row without the names blanked the group chip
    until reload).

    WHY server-side name resolution: a moderator's scoped list never contains
    the moderator (or the admin who created a member), so a client-side lookup
    over the loaded rows renders bare uids. One query over the referenced ids —
    never an N+1 — regardless of caller role."""
    names = await _names_of(db, {u.get("created_by") for u in users} | {u.get("moderator_id") for u in users})
    for u in users:
        u["created_by_name"] = names.get(u.get("created_by"))
        u["moderator_name"] = names.get(u.get("moderator_id"))
    return users


async def _names_of(db: AsyncSurreal, ids: set[str | None]) -> dict[str, str]:
    """uid → display name for every existing users row in `ids` (None ignored)."""
    wanted = [i for i in ids if i]
    if not wanted:
        return {}
    rows = await db.query(
        "SELECT meta::id(id) AS uid, name FROM users WHERE meta::id(id) IN $ids",
        {"ids": wanted},
    )
    return {r["uid"]: r["name"] for r in (rows or [])}


@router.post("/api/admin/users")
async def create_user_admin(body: CreateUserAdmin, manager: dict = Depends(require_user_manager_role), db: AsyncSurreal = Depends(get_db)):
    """Create a user. Admin: role + moderator_id from the body; moderator: both are
    forced (role='user', moderator_id=<self>) regardless of the body."""
    if manager.get("role") == "moderator":
        role, moderator_id = "user", manager["user_id"]
    else:
        role, moderator_id = body.role, body.moderator_id
        if moderator_id is not None:
            mod_row = await fetch_one("users", moderator_id)
            if not mod_row or mod_row.get("role") != "moderator":
                raise HTTPException(status_code=422, detail="moderator_id must point at a moderator")
    if moderator_id is not None and role != "user":
        raise HTTPException(status_code=422, detail="moderator_id is only valid on role='user'")
    dup = await db.query(
        "SELECT id FROM users WHERE email = $e AND deleted_at IS NONE LIMIT 1",
        {"e": body.email},
    )
    if dup:
        raise HTTPException(status_code=409, detail="Email already in use")
    uid = str(uuid4())
    record = await create_record("users", uid, {
        "name": body.name,
        "email": body.email,
        "password_hash": hash_secret(body.password),
        "role": role,
        "user_facts": "",
        # created_by: provenance for the moderator's group — which admin created
        # this account. Nothing reads it yet; it cannot be backfilled later.
        "created_by": manager["user_id"],
        "moderator_id": moderator_id,
    })
    await _flush_pending_invites(body.email, uid)
    return (await _with_names(db, [serialize_record(record, "user_id")]))[0]


@router.put("/api/users/{user_id}")
async def update_user(user_id: str, body: UpdateUser, current_user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Update user's personal facts and/or auto-detected timezone. Users can only edit their own."""
    if user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Cannot edit another user")
    updates: dict = {}
    if body.user_facts is not None:
        updates["user_facts"] = body.user_facts
    if body.timezone is not None:
        updates["timezone"] = body.timezone
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")
    # NOTE: set_clause keys are hardcoded ("user_facts", "timezone"), not user input — safe from injection
    set_parts = [f"{k} = ${k}" for k in updates] + ["updated_at = time::now()"]
    set_clause = ", ".join(set_parts)
    rows = await db.query(
        f"UPDATE type::record('users', $id) SET {set_clause} RETURN AFTER",
        {"id": user_id, **updates},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="User not found")
    return serialize_record(rows[0], "user_id")


@router.patch("/api/admin/users/{user_id}")
async def patch_user_admin(user_id: str, body: UpdateUserAdmin, current_user: dict = Depends(require_user_manager), db: AsyncSurreal = Depends(get_db)):
    """Admin: update anything (manager + admin fields). Moderator: manager fields of an
    own-group user only — a body touching ADMIN_FIELDS is a 403, and role transitions
    follow the flat-group rules (_collect_admin_field_updates)."""
    if current_user.get("role") != "admin":
        if [f for f in ADMIN_FIELDS if f in body.model_fields_set]:
            raise HTTPException(status_code=403, detail="Admin access required")

    target = await fetch_one("users", user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        updates = await _collect_admin_field_updates(db, target, body, current_user, user_id)
    except _GroupNotEmpty as exc:
        return _group_not_empty(exc.moderator_uid, exc.n)
    updates.update(_manager_field_updates(body))
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")
    row = await _apply_user_updates(db, user_id, updates)
    # INVARIANT: a credential reset (password change) must invalidate the target's existing
    # sessions immediately. Why: without this the target's in-flight cookies stay valid for
    # up to JWT TTL (14 days) + token_version cache TTL on the old credential. Bumping the
    # token version fans out cross-replica via the event_bus so every replica rejects the
    # old token on the next request.
    if body.password is not None:
        await bump_token_version(user_id)
    # ARCH: a role change must NOT log the target out — it refreshes the role
    # cache on every replica (token_version_bumped event without a version
    # bump), so the very next request on the OLD cookie is authorized under the
    # new role (role overlay in auth.get_current_user).
    if "role" in updates:
        await refresh_user_role_cache(user_id)
    return (await _with_names(db, [serialize_record(row, "user_id")]))[0]


@router.delete("/api/admin/users/{user_id}")
async def delete_user(user_id: str, current_user: dict = Depends(require_user_manager), db: AsyncSurreal = Depends(get_db)):
    """Admin/moderator: soft-delete a user. Cannot delete self; a moderator with
    active group members must have them reassigned first (409, group_size)."""
    if current_user["user_id"] == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    target = await fetch_one("users", user_id)
    if target and target.get("role") == "moderator":
        n = await _group_size(db, user_id)
        if n:
            return _group_not_empty(user_id, n)
    # INVARIANT(data): soft-delete RELEASES the email. Why: idx_users_email is
    # UNIQUE over ALL rows — the index does not see deleted_at — so a
    # tombstone would keep the address reserved forever and re-creating it
    # would 500 on the index (the duplicate 409 check only reads live rows).
    # The tombstone keeps the original address readable, mangled with the uid
    # (unique per row) for audits.
    if target and target.get("email"):
        await db.query(
            "UPDATE type::record('users', $id) SET email = $released",
            {"id": user_id, "released": f"deleted:{user_id}:{target['email']}"},
        )
    await soft_delete("users", user_id)
    return {"success": True}


@router.get("/api/admin/users/{user_id}/project-access")
async def get_user_project_access(user_id: str, _: dict = Depends(require_user_manager), db: AsyncSurreal = Depends(get_db)):
    """Admin: return project_id → access_level map for a user's explicit memberships."""
    rows = await db.query(
        "SELECT project_id, access_level FROM project_members WHERE user_id = $uid",
        {"uid": user_id},
    )
    return {r["project_id"]: r["access_level"] for r in (rows or [])}
