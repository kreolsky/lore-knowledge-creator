"""Model access — which gateway models a user may run, from public / moderator-group / admin-group grants.

# SYSTEM: model-access — per-user model availability (grants to public, moderator groups, admin groups) and the turn gate
# ARCH: ONE table carries all access — model_grants(model_id, subject); a subject
# is 'public', 'mod:<moderator uid>' or 'group:<admin group id>'. "Public" is a
# grant to the subject 'public', not a flag. The moderator group is VIRTUAL:
# derived from users.moderator_id, never stored, so promotion, user creation,
# invite registration and a moderator_id change run no code here.
# ARCH: ONE enforcement point — require_model_access, called by the turn
# (routes/chat/completions.py _validate_turn_and_model) after the model is
# resolved, so an explicit, session-pinned or inherited model and a grant
# revoked after pinning are all refused there. GET /api/chat/models only
# FILTERS the picker. System consumers (CHAT_QUERY_REWRITE_MODEL,
# COMFYUI_PROMPT_MODEL, EMBEDDING_MODEL, …) read config and never pass this gate.
# Only an admin reads or writes groups and grants (routes/admin_model_access.py).
"""

from __future__ import annotations

import settings
from fastapi import HTTPException
from surrealdb import AsyncSurreal

from access import is_instance_admin
from db import get_db

PUBLIC_SUBJECT = "public"
MOD_PREFIX = "mod:"
GROUP_PREFIX = "group:"


def moderator_subject(moderator_uid: str) -> str:
    return f"{MOD_PREFIX}{moderator_uid}"


def group_subject(group_id: str) -> str:
    return f"{GROUP_PREFIX}{group_id}"


async def user_subjects(db: AsyncSurreal, user_id: str) -> list[str]:
    """The grant subjects a live user holds; [] for a missing or soft-deleted row.

    `mod:<own uid>` for a moderator: promotion clears the moderator's own
    moderator_id, so without it a moderator would miss their own group's models.
    """
    rows = await db.query(
        "SELECT role, moderator_id, deleted_at FROM type::record('users', $id)",
        {"id": user_id},
    )
    if not rows or rows[0].get("deleted_at") is not None:
        return []
    row = rows[0]
    subjects = [PUBLIC_SUBJECT]
    if row.get("moderator_id"):
        subjects.append(moderator_subject(row["moderator_id"]))
    if row.get("role") == "moderator":
        subjects.append(moderator_subject(user_id))
    group_ids = await db.query(
        "SELECT VALUE group_id FROM group_members WHERE user_id = $id", {"id": user_id},
    )
    subjects.extend(group_subject(g) for g in (group_ids or []))
    return subjects


async def available_models(user: dict) -> set[str] | None:
    """Model ids the user may run; None = every model (admin)."""
    if is_instance_admin(user):
        return None
    db = await get_db()
    subjects = await user_subjects(db, user["user_id"])
    granted = await db.query(
        "SELECT VALUE model_id FROM model_grants WHERE subject IN $subjects",
        {"subjects": subjects},
    ) if subjects else []
    allowed = set(granted or [])
    # WHY: the resolved default model is available to EVERY user, with no grant
    # row, and follows a CHAT_MODEL change — the turn falls back to CHAT_MODEL,
    # so a private default would refuse every fresh chat.
    default_model = await settings.get("CHAT_MODEL")
    if default_model:
        allowed.add(default_model)
    return allowed


async def require_model_access(user: dict, model: str) -> None:
    """403 `model_forbidden` when the user may not run `model`.

    Never swaps a refused model for the default: the client shows the error
    and opens the picker.
    """
    allowed = await available_models(user)
    if allowed is None or model in allowed:
        return
    raise HTTPException(status_code=403, detail={"code": "model_forbidden", "model": model})


async def drop_moderator_grants(db: AsyncSurreal, moderator_uid: str) -> None:
    """Delete the virtual group's grants once its moderator is demoted or deleted.

    A re-promoted moderator starts with no grants; nothing is restored.
    """
    await db.query(
        "DELETE model_grants WHERE subject = $subject",
        {"subject": moderator_subject(moderator_uid)},
    )
