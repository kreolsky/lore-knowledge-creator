"""Shared grant service — the single write path for "email → membership or
pending invite".

# ARCH: One coherent invite path. Resolves email, applies the owner guard, then
# either writes a project_members grant via the check-then-update `_apply_grant`
# core, or queues a `pending_invites` row.
"""
# SYSTEM: access-grants — single owner-guarded grant write path

from __future__ import annotations

from uuid import uuid4

from surrealdb import AsyncSurreal

import event_bus
from access import get_user_by_email
from db import create_record, extract_id, get_db


async def _delete_pending_by_scope(db, email: str, project_id: str) -> list[dict]:
    """Delete pending_invites rows matching (email, project_id).

    Returns the deleted rows (for audit/logging). ARCH: planner-safe — a
    compound `WHERE email = $e AND project_id = $p` over multiple indexed
    fields hits a SurrealDB query-planner bug (see access.py note). Fetch by
    the email index, filter the scope in Python, then a single bulk
    `DELETE ... WHERE id IN $ids`. The UNIQUE index (idx_pi_unique) is the
    final guard. Single source of truth for the grant upsert AND the cancel
    endpoint so the two cannot diverge.
    """
    candidates = await db.query(
        "SELECT id, project_id FROM pending_invites WHERE email = $e",
        {"e": email},
    )
    target_ids = [
        extract_id(r["id"])
        for r in (candidates or [])
        if r.get("project_id") == project_id
    ]
    deleted: list[dict] = []
    if target_ids:
        # ARCH: param-bound per-id record refs in an IN list. A bare `id IN $ids`
        # with string ids matches nothing (the id field is a record ref), and
        # `type::record('pending_invites', $ids)` with a list doesn't bulk-delete
        # either — only per-id record refs work. ids come from our own extract_id
        # of our own rows, so this is param-safe (no f-string interpolation).
        refs = [f"type::record('pending_invites', $id{i})" for i in range(len(target_ids))]
        params = {f"id{i}": tid for i, tid in enumerate(target_ids)}
        deleted = await db.query(
            f"DELETE FROM pending_invites WHERE id IN [{', '.join(refs)}] RETURN BEFORE",
            params,
        )
    return deleted or []


async def grant_or_queue(
    *, project_id: str, email: str,
    access_level: str, invited_by: str, owner_id: str | None,
    db: AsyncSurreal | None = None,
) -> dict:
    """Resolve `email` → grant write or queued pending invite.

    `owner_id` is the target project's owner (already known to callers via
    require_project_owner); pass None to skip the owner guard.

    # INVARIANT(security): Silent invite — identical response whether the email is
    # registered or queued. Why: prevents enumeration of registered users via
    # the invite endpoint (security contract).
    # INVARIANT(security): owner is never a member row. Why: owner access is
    # implied by projects.owner_id; a row would only leak into GET /members.
    """
    db = db if db is not None else await get_db()
    target = await get_user_by_email(email)
    if not target:
        # Queue pending invite (upsert). _delete_pending_by_scope handles the
        # planner-safe delete-by-composite-key, then CREATE.
        email_l = email.lower()
        await _delete_pending_by_scope(db, email_l, project_id)
        await create_record("pending_invites", str(uuid4()), {
            "email": email_l,
            "project_id": project_id,
            "access_level": access_level,
            "invited_by": invited_by,
        })
        return {"queued": True}

    target_uid = target["user_id"] if "user_id" in target else extract_id(target.get("id"))
    # Owner guard: a real owner must never materialize as a membership row.
    if owner_id is not None and target_uid == owner_id:
        # INVARIANT: owner is never a member row; Why: owner access
        # implied by owner_id (access.get_project_access returns 'full').
        return {"queued": True}

    await _apply_grant(
        project_id=project_id, target_uid=target_uid,
        access_level=access_level, db=db,
    )
    return {"queued": True}


async def _apply_grant(
    *, project_id: str, target_uid: str,
    access_level: str, db: AsyncSurreal | None = None,
) -> None:
    """Pre-resolved grant core: upsert project_members via check-then-update.

    # ARCH: check-then-update on the composite UNIQUE index — the DB would
    # otherwise raise on a pre-existing row (idx_pm_unique).
    # Used directly by `_flush_pending_invites` which already holds a resolved uid.
    """
    db = db if db is not None else await get_db()

    existing = await db.query(
        "SELECT id FROM project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": project_id, "uid": target_uid},
    )
    if existing:
        pm_id = extract_id(existing[0]["id"])
        await db.query(
            "UPDATE type::record('project_members', $id) SET access_level = $al",
            {"id": pm_id, "al": access_level},
        )
    else:
        await create_record("project_members", str(uuid4()), {
            "project_id": project_id,
            "user_id": target_uid,
            "access_level": access_level,
        })
    await event_bus.emit("access_changed", project_id=project_id, user_id=target_uid, access=access_level)
