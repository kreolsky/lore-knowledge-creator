"""Admin model-access routes — model grants and groups (see SYSTEM: model-access).

# ARCH: every route is require_admin — a moderator gets no group/grant surface;
# they see their models in the picker and their users in the existing user list.
# Moderator groups are VIRTUAL (derived from users.moderator_id): GET /groups
# lists them read-only, and every write route takes an admin group id only, so a
# `mod:` id is a 404.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import settings
from fastapi import APIRouter, Depends, HTTPException
from model_access import (
    GROUP_PREFIX,
    MOD_PREFIX,
    PUBLIC_SUBJECT,
    group_subject,
    moderator_subject,
)
from pydantic import BaseModel, Field
from surrealdb import AsyncSurreal

from auth import require_admin
from db import create_record, get_db, run_in_transaction
from routes.chat import gateway_model_ids

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin-model-access"])


class ModelSubjects(BaseModel):
    subjects: list[str]


class GroupName(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class GroupMembers(BaseModel):
    user_ids: list[str]


async def _grants_by_model(db: AsyncSurreal) -> dict[str, list[str]]:
    rows = await db.query("SELECT model_id, subject FROM model_grants")
    out: dict[str, list[str]] = {}
    for r in rows or []:
        out.setdefault(r["model_id"], []).append(r["subject"])
    return out


async def _validate_subjects(db: AsyncSurreal, subjects: list[str]) -> None:
    """422 for a malformed subject, a `mod:` uid that is not a live moderator,
    or a `group:` id that does not exist."""
    mod_uids = [s.removeprefix(MOD_PREFIX) for s in subjects if s.startswith(MOD_PREFIX)]
    group_ids = [s.removeprefix(GROUP_PREFIX) for s in subjects if s.startswith(GROUP_PREFIX)]
    malformed = [
        s for s in subjects
        if s != PUBLIC_SUBJECT and not s.startswith((MOD_PREFIX, GROUP_PREFIX))
    ]
    live_mods = await db.query(
        "SELECT VALUE meta::id(id) FROM users WHERE meta::id(id) IN $ids "
        "AND role = 'moderator' AND deleted_at IS NONE",
        {"ids": mod_uids},
    ) if mod_uids else []
    live_groups = await db.query(
        "SELECT VALUE meta::id(id) FROM groups WHERE meta::id(id) IN $ids", {"ids": group_ids},
    ) if group_ids else []
    unknown = malformed + [
        moderator_subject(u) for u in mod_uids if u not in set(live_mods or [])
    ] + [group_subject(g) for g in group_ids if g not in set(live_groups or [])]
    if unknown:
        raise HTTPException(status_code=422, detail={"code": "unknown_subject", "subjects": unknown})


@router.get("/models")
async def list_model_grants(_: dict = Depends(require_admin), db: AsyncSurreal = Depends(get_db)):
    """Gateway roster ∪ granted ids → [{id, in_gateway, is_default, subjects}].

    `in_gateway` is None on every row when the gateway cannot be read: the grants
    stay listed and revocable during a gateway outage, and the client names the
    outage instead of marking every model "not in gateway"."""
    try:
        roster: set[str] | None = set(await gateway_model_ids())
    except Exception:
        logger.warning("admin models: gateway roster unreadable", exc_info=True)
        roster = None
    grants = await _grants_by_model(db)
    default_model = await settings.get("CHAT_MODEL")
    ids = (roster or set()) | set(grants)
    return [
        {
            "id": mid,
            "in_gateway": None if roster is None else mid in roster,
            "is_default": mid == default_model,
            "subjects": sorted(grants.get(mid, [])),
        }
        for mid in sorted(ids)
    ]


@router.put("/models/{model_id:path}")
async def put_model_grants(
    model_id: str, body: ModelSubjects,
    _: dict = Depends(require_admin), db: AsyncSurreal = Depends(get_db),
):
    """Full replace of a model's subjects. A kept subject keeps its row; a
    revoked one is deleted, so granting it again creates a fresh row."""
    subjects = list(dict.fromkeys(body.subjects))
    await _validate_subjects(db, subjects)
    current = await db.query(
        "SELECT VALUE subject FROM model_grants WHERE model_id = $m", {"m": model_id},
    ) or []
    removed = [s for s in current if s not in subjects]
    added = [s for s in subjects if s not in current]
    statements: list[str] = []
    params: dict = {"m": model_id, "removed": removed}
    if removed:
        statements.append("DELETE model_grants WHERE model_id = $m AND subject IN $removed")
    for i, subject in enumerate(added):
        statements.append(f"CREATE model_grants CONTENT {{ model_id: $m, subject: $s{i} }}")
        params[f"s{i}"] = subject
    if statements:
        await run_in_transaction(db, statements, params)
    return {"id": model_id, "subjects": sorted(subjects)}


# ─── Groups ───────────────────────────────────────────────────────────────────


def _group_payload(
    gid: str, kind: str, name: str, members: list[dict], grants: dict[str, list[str]],
) -> dict:
    subject = moderator_subject(gid) if kind == "moderator" else group_subject(gid)
    return {
        "id": subject if kind == "moderator" else gid,
        "subject": subject,
        "kind": kind,
        "name": name,
        "members": members,
        "models": sorted(m for m, subs in grants.items() if subject in subs),
    }


@router.get("/groups")
async def list_groups(_: dict = Depends(require_admin), db: AsyncSurreal = Depends(get_db)):
    """Admin groups, then one read-only entry per live moderator (kind='moderator',
    name = the moderator's name — the client renders the "Moderator · " label)."""
    groups = await db.query(
        "SELECT meta::id(id) AS id, name, created_at FROM groups ORDER BY created_at ASC",
    ) or []
    users = await db.query(
        "SELECT meta::id(id) AS uid, name, role, moderator_id, created_at FROM users "
        "WHERE deleted_at IS NONE ORDER BY created_at ASC",
    ) or []
    memberships = await db.query("SELECT group_id, user_id FROM group_members") or []
    grants = await _grants_by_model(db)
    by_uid = {u["uid"]: u for u in users}

    def member(uid: str) -> dict:
        return {"user_id": uid, "name": by_uid[uid]["name"]}

    out = [
        _group_payload(g["id"], "admin", g["name"], [
            member(m["user_id"]) for m in memberships
            if m["group_id"] == g["id"] and m["user_id"] in by_uid
        ], grants)
        for g in groups
    ]
    out.extend(
        _group_payload(mod["uid"], "moderator", mod["name"], [
            member(u["uid"]) for u in users if u.get("moderator_id") == mod["uid"]
        ], grants)
        for mod in users if mod.get("role") == "moderator"
    )
    return out


async def _require_admin_group(db: AsyncSurreal, group_id: str) -> dict:
    """The admin group row, or 404 (a `mod:` id is never a stored group)."""
    rows = await db.query(
        "SELECT meta::id(id) AS id, name FROM type::record('groups', $id)", {"id": group_id},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Group not found")
    return rows[0]


async def _single_group(db: AsyncSurreal, group_id: str) -> dict:
    full = await list_groups({}, db)
    group = next((g for g in full if g["kind"] == "admin" and g["id"] == group_id), None)
    if group is None:  # deleted by a concurrent request after this one's check
        raise HTTPException(status_code=404, detail="Group not found")
    return group


@router.post("/groups")
async def create_group(
    body: GroupName, _: dict = Depends(require_admin), db: AsyncSurreal = Depends(get_db),
):
    gid = str(uuid4())
    await create_record("groups", gid, {"name": body.name})
    return await _single_group(db, gid)


@router.patch("/groups/{group_id}")
async def rename_group(
    group_id: str, body: GroupName,
    _: dict = Depends(require_admin), db: AsyncSurreal = Depends(get_db),
):
    await _require_admin_group(db, group_id)
    await db.query(
        "UPDATE type::record('groups', $id) SET name = $name, updated_at = time::now()",
        {"id": group_id, "name": body.name},
    )
    return await _single_group(db, group_id)


@router.delete("/groups/{group_id}")
async def delete_group(
    group_id: str, _: dict = Depends(require_admin), db: AsyncSurreal = Depends(get_db),
):
    """Hard delete: the group, its membership rows and its `group:<id>` grants."""
    await _require_admin_group(db, group_id)
    await run_in_transaction(db, [
        "DELETE group_members WHERE group_id = $id",
        "DELETE model_grants WHERE subject = $subject",
        "DELETE type::record('groups', $id)",
    ], {"id": group_id, "subject": group_subject(group_id)})
    return {"success": True}


@router.put("/groups/{group_id}/members")
async def put_group_members(
    group_id: str, body: GroupMembers,
    _: dict = Depends(require_admin), db: AsyncSurreal = Depends(get_db),
):
    """Full replace of an admin group's members; every id must be a live user (422)."""
    await _require_admin_group(db, group_id)
    user_ids = list(dict.fromkeys(body.user_ids))
    live = set(await db.query(
        "SELECT VALUE meta::id(id) FROM users WHERE meta::id(id) IN $ids AND deleted_at IS NONE",
        {"ids": user_ids},
    ) or []) if user_ids else set()
    unknown = [u for u in user_ids if u not in live]
    if unknown:
        raise HTTPException(status_code=422, detail={"code": "unknown_user", "user_ids": unknown})
    statements = ["DELETE group_members WHERE group_id = $id"]
    params: dict = {"id": group_id}
    for i, uid in enumerate(user_ids):
        statements.append(f"CREATE group_members CONTENT {{ group_id: $id, user_id: $u{i} }}")
        params[f"u{i}"] = uid
    await run_in_transaction(db, statements, params)
    return await _single_group(db, group_id)
