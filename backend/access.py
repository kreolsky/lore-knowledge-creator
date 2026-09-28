"""Project access control — guards for route handlers."""
# ARCH: Access cascade: owner→full, member→lookup, public→readonly. An admin
# WITH a project_members row is elevated to full on both the membership and the
# public branch (admin-member = project root). An admin WITHOUT a row gets what
# the project already offers everyone: full on the public branch, None on a
# private project. require_admin_or_project_full stays the admin-override
# management gate. A document inherits its project's access level.

from fastapi import HTTPException

import db as db_mod
from db import extract_id, fetch_doc_meta, fetch_one


async def get_project_access(project_id: str, user: dict) -> str | None:
    """Determine the current user's access level for a project.

    Returns 'full', 'commentator', 'readonly', or None (no access / not found).
    """
    project = await fetch_one("projects", project_id)
    if not project:
        return None
    uid = user["user_id"]
    if project.get("owner_id") == uid:
        return "full"
    db = await db_mod.get_db()
    # NOTE: LIMIT 1 with compound AND WHERE triggers a SurrealDB query-planner bug
    # (the idx_pm_unique composite index causes empty results). Fetch without LIMIT.
    member_rows = await db.query(
        "SELECT access_level FROM project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": project_id, "uid": uid},
        site="access_check",
    )
    if member_rows:
        # ARCH: an admin with a member row is elevated to 'full' — the member row
        # is the explicit act of joining a project, and the admin's instance role
        # tops up the joined access (never the reverse: elevation is gated on the
        # row existing, so a row is required on private projects).
        if is_instance_admin(user):
            return "full"
        return member_rows[0]["access_level"]
    if project.get("is_public"):
        return "full" if is_instance_admin(user) else "readonly"
    return None


def is_instance_admin(user: dict) -> bool:
    """Whether the user carries the instance-admin role.

    # WHY: the instance role gains a third value (moderator) later; every
    # authorization read was `role != "admin"`, correct only by accident once a
    # third value exists. This is the one place that comparison lives.
    """
    return user.get("role") == "admin"


async def is_project_root(project: dict, user: dict) -> bool:
    """Whether the user is a project root: the owner, or an admin with a
    project_members row.

    # ARCH: the member row is the
    # explicit act of joining a project — project administration follows the
    # join, not the instance role. So an admin edits documents on a public
    # project (public branch → 'full') but cannot invite members or mint share
    # links there: the owner surface requires the row, `full` write does not.
    """
    if not project:
        return False
    if project.get("owner_id") == user["user_id"]:
        return True
    if not is_instance_admin(user):
        return False
    project_id = project.get("project_id") or extract_id(project.get("id")) or ""
    if not project_id:
        return False
    db = await db_mod.get_db()
    # NOTE: same no-LIMIT rule as get_project_access (SurrealDB planner bug).
    member_rows = await db.query(
        "SELECT access_level FROM project_members WHERE project_id = $pid AND user_id = $uid",
        {"pid": project_id, "uid": user["user_id"]},
        site="access_check",
    )
    return bool(member_rows)


async def require_project_full(project_id: str, user: dict) -> None:
    """Raise 403/404 unless user has full access to the project."""
    access = await get_project_access(project_id, user)
    if access is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if access != "full":
        raise HTTPException(status_code=403, detail="Write access required")


async def require_project_read(project_id: str, user: dict) -> None:
    """Raise 403/404 unless user has at least read access to the project."""
    access = await get_project_access(project_id, user)
    if access is None:
        raise HTTPException(status_code=404, detail="Project not found")


async def get_doc_project_id(document_id: str) -> str | None:
    """Look up the project_id of a document (None if not found).

    # ARCH: uses the projected fetch_doc_meta (no ydoc_state/content transfer) —
    # this is the hottest access path, called for every get_document_access.
    """
    doc = await fetch_doc_meta(document_id)
    return doc.get("project_id") if doc else None


def can_modify_note_message(
    *, action: str, user_id: str, message: dict, session: dict,
    is_owner: bool = False, has_children: bool = False,
) -> bool:
    """ACL for note-chat message edits/deletes.

    INVARIANT (own-only + root model):
      - 'edit': the message author OR a project root (the owner, or an admin
        with a member row). The root may edit any note message.
      - 'delete': a project root (cascade over replies) OR the author
        of a LEAF message (no replies) → single row.
    The old `access_level == 'full'` moderation override is REMOVED — full-access
    non-roots can no longer delete other users' content. Only a root is root.

    The structural "author of a non-leaf" case returns False here; the route
    maps it to 409 (Cannot delete a message that has replies) to give a precise
    error, distinct from the 403 a non-author receives.

    Falls back to session.user_id when message.author_id is NONE
    (see resolve_message_author in models.py).
    """
    from models import resolve_message_author
    if action not in ("edit", "delete"):
        return False
    author = resolve_message_author(message, session)
    is_author = author == user_id
    if action == "edit":
        return is_author or is_owner
    # delete
    if is_owner:
        return True
    return is_author and not has_children


async def require_admin_or_project_full(project_id: str, user: dict) -> None:
    """Raise 403/404 unless user is admin or has full project access."""
    if not is_instance_admin(user):
        await require_project_full(project_id, user)
    else:
        if not await fetch_one("projects", project_id):
            raise HTTPException(status_code=404, detail="Project not found")


# ─── Document-level access (= project access) ───────────────────────────────

async def get_document_access(document_id: str, user: dict) -> str | None:
    """Resolve a user's access level for a single document.

    # INVARIANT(security): document access IS project access — there is no
    # per-document override. Why: operator ruling "Нет, убираем" — one access
    # level per project; the owner keeps 'full' through get_project_access.
    """
    project_id = await get_doc_project_id(document_id)
    if project_id is None:
        return None
    return await get_project_access(project_id, user)


async def require_document_read(document_id: str, user: dict) -> None:
    """Raise 403/404 unless user has at least read access to the document."""
    access = await get_document_access(document_id, user)
    if access is None:
        raise HTTPException(status_code=404, detail="Document not found")


async def require_document_full(document_id: str, user: dict) -> None:
    """Raise 403/404 unless user has full access to the document."""
    access = await get_document_access(document_id, user)
    if access is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if access != "full":
        raise HTTPException(status_code=403, detail="Write access required")


# ─── Ownership gate (manages membership) ────────────────────────────────────

async def require_project_owner(project: dict, user: dict) -> None:
    """Raise 403 unless `user` is a project root: the owner, or an admin with a
    project_members row.

    # INVARIANT(security): Only a project root manages membership.
    # Why: industry standard (GitHub/Notion); avoids mutual revoke between full
    # collaborators. The member row is the explicit act of joining a project —
    # project administration follows the join, not the instance role, so an
    # admin without a row gets `full` write on a public project but no owner
    # surface (invite/change/remove members, share links).
    """
    if not await is_project_root(project, user):
        raise HTTPException(status_code=403, detail="Only the project owner can manage access")


async def ensure_owner_for_doc(project_id: str, document_id: str, user: dict) -> dict:
    """Fetch the project (404 if missing), enforce the owner gate, validate the
    doc belongs to this project (uniform 404 — no existence oracle). Returns project.

    Used by the document-shares routes: the owner guard plus the cross-project
    check in one place.
    """
    project = await fetch_one("projects", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_project_owner(project, user)
    doc = await fetch_one("documents", document_id)
    if not doc or doc.get("deleted_at") or doc.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="Document not found")
    return project


# ─── Email lookup helper ────────────────────────────────────────────────────

async def get_user_by_email(email: str) -> dict | None:
    """Look up a non-deleted user by email (case-insensitive)."""
    db = await db_mod.get_db()
    rows = await db.query(
        "SELECT * FROM users WHERE email = $e AND deleted_at IS NONE LIMIT 1",
        {"e": email.strip().lower()},
        site="access_check",
    )
    return rows[0] if rows else None
