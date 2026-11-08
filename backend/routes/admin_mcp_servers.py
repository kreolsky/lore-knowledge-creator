"""Admin MCP server routes — definitions each user keys themselves (SYSTEM: mcp-servers).

# ARCH: the role gate is require_user_manager_role (admin | moderator — the
# same gate as the admin user routes); the moderator SCOPE is this module's
# own: a moderator may create or edit only servers whose SINGLE subject is
# their own virtual group (mod:<uid>), sees only those in the list, and gets
# the require_moderator_scope 404 posture on anything else — a moderator must
# not learn that a foreign server exists. An admin may grant any subject of
# the model-access vocabulary.
"""

from __future__ import annotations

import mcp_servers
import user_secrets
from fastapi import APIRouter, Depends, HTTPException
from model_access import (
    GROUP_PREFIX,
    MOD_PREFIX,
    PUBLIC_SUBJECT,
    group_subject,
    moderator_subject,
)
from pydantic import BaseModel, Field
from security_log import log_security
from surrealdb import AsyncSurreal

from auth import require_user_manager_role
from db import extract_id, get_db

router = APIRouter(prefix="/api/admin/mcp-servers", tags=["admin-mcp-servers"])


class McpServerCreate(BaseModel):
    name: str
    title: str = ""
    url: str
    auth_mode: str = "personal"
    auth_scheme: str = "Bearer"
    help_url: str | None = None
    #: Write-only shared key (auth_mode='shared' only); never in a response.
    shared_key: str | None = Field(default=None, max_length=8192)
    subjects: list[str] = Field(default_factory=lambda: [PUBLIC_SUBJECT])
    enabled: bool = True


class McpServerUpdate(BaseModel):
    """PATCH body — every field optional; a provided url re-runs the egress
    check, and stored keys keep following the OLD host until each user
    re-enters theirs (the "re-enter key" state is the point). A mode change
    away from `shared` clears the shared ciphertext; away from `personal` it
    deletes every user's key rows (an orphaned ciphertext row is a credential
    left in the database)."""
    name: str | None = None
    title: str | None = None
    url: str | None = None
    auth_mode: str | None = None
    auth_scheme: str | None = None
    help_url: str | None = None
    shared_key: str | None = Field(default=None, max_length=8192)
    subjects: list[str] | None = None
    enabled: bool | None = None


async def _validate_subjects(db: AsyncSurreal, subjects: list[str]) -> list[str]:
    """422 for an empty/malformed subject list, a `mod:` uid that is not a
    live moderator, or a `group:` id that does not exist — the admin_model_access
    vocabulary minus the model-only tiers (hidden/tools grant nothing here)."""
    if not subjects:
        raise HTTPException(status_code=422, detail={"code": "no_subjects"})
    malformed = [
        s for s in subjects
        if s != PUBLIC_SUBJECT and not s.startswith((MOD_PREFIX, GROUP_PREFIX))
    ]
    mod_uids = [s.removeprefix(MOD_PREFIX) for s in subjects if s.startswith(MOD_PREFIX)]
    group_ids = [s.removeprefix(GROUP_PREFIX) for s in subjects if s.startswith(GROUP_PREFIX)]
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
    return list(dict.fromkeys(subjects))


def _own_mod_subject(user: dict) -> str:
    return moderator_subject(user["user_id"])


def _is_own_group_server(row: dict, user: dict) -> bool:
    """Exactly the moderator's writable set: the single subject is mod:<own>."""
    return row.get("subjects") == [_own_mod_subject(user)]


async def _scoped_row(db: AsyncSurreal, user: dict, server_id: str) -> dict:
    """The row, or 404 — for a moderator a foreign server does not exist."""
    row = await mcp_servers.get_row(db, server_id)
    if not row:
        raise HTTPException(status_code=404, detail="Server not found")
    if user.get("role") == "moderator" and not _is_own_group_server(row, user):
        raise HTTPException(status_code=404, detail="Server not found")
    return row


def _admin_view(row: dict, creator_name: str = "") -> dict:
    """The admin REST projection. The shared key answers ONLY as
    shared_key_set/shared_needs_reenter — never a value (the same write-only
    contract as a personal key in the cabinet)."""
    host, port = "", None
    try:
        host, port = mcp_servers.parse_server_url(row.get("url") or "")
    except mcp_servers.McpServerError:
        pass
    mode = row.get("auth_mode") or "personal"
    shared_set = bool(
        mode == "shared" and user_secrets.is_set(row.get("shared_ciphertext") or "")
    )
    return {
        "id": extract_id(row["id"]),
        "name": row.get("name"),
        "title": row.get("title"),
        "url": row.get("url"),
        "host": (f"{host}:{port}" if port else host),
        "auth_mode": mode,
        "auth_scheme": row.get("auth_scheme") or "",
        "help_url": row.get("help_url"),
        "subjects": list(row.get("subjects") or []),
        "created_by": row.get("created_by"),
        "created_by_name": creator_name,
        "enabled": bool(row.get("enabled")),
        "shared_key_set": shared_set,
        # A url move to another host strands the stored key: it is bound to
        # the host it was saved against.
        "shared_needs_reenter": bool(
            shared_set and host
            and row.get("shared_host") != user_secrets.host_key(host, port)
        ),
    }


async def _with_creator_names(db: AsyncSurreal, rows: list[dict]) -> list[dict]:
    uids = sorted({r.get("created_by") for r in rows if r.get("created_by")})
    names = {
        r["uid"]: r["name"]
        for r in await db.query(
            "SELECT meta::id(id) AS uid, name FROM users WHERE meta::id(id) IN $ids",
            {"ids": uids},
        ) or []
    }
    return [_admin_view(r, names.get(r.get("created_by"), "")) for r in rows]


def _shared_key_sets(db_fields: dict, body_key: str | None) -> dict:
    """The shared-key column updates of one create/patch, or {}. The value is
    normalized through the same gate as a personal key, encrypted HERE, and
    bound to the CURRENT url's host at save time."""
    if body_key is None:
        return {}
    value = mcp_servers.normalize_key(body_key, db_fields["auth_scheme"])
    return {
        "shared_ciphertext": user_secrets.encrypt_value(value),
        "shared_host": user_secrets.host_key(db_fields["host"], db_fields["port"]),
    }


async def _validated_fields(
    db: AsyncSurreal, body: McpServerUpdate, current: dict,
) -> dict:
    """Merge the PATCH onto the stored row and validate the merged definition
    (+ the egress check when url moves). Returns the normalized row fields."""
    merged = mcp_servers.validate_definition(
        name=body.name or current.get("name"),
        title=current.get("title") if body.title is None else body.title,
        url=current.get("url") if body.url is None else body.url,
        auth_mode=(
            current.get("auth_mode") or "personal"
            if body.auth_mode is None else body.auth_mode
        ),
        auth_scheme=(
            current.get("auth_scheme") if body.auth_scheme is None else body.auth_scheme
        ),
        help_url=current.get("help_url") if body.help_url is None else body.help_url,
    )
    if body.url is not None and body.url != current.get("url"):
        await mcp_servers.check_egress(merged["host"])
    return merged


def _mode_change_sets(merged: dict, current: dict) -> tuple[dict, bool]:
    """(column updates, purge_personal_rows) of a mode change: leaving
    `shared` clears the ciphertext pair; leaving `personal` purges every
    user's key rows (an orphaned ciphertext is a credential left in the
    database)."""
    old_mode = current.get("auth_mode") or "personal"
    new_mode = merged["auth_mode"]
    sets: dict = {}
    if old_mode == "shared" and new_mode != "shared":
        sets["shared_ciphertext"] = None
        sets["shared_host"] = None
    return sets, old_mode == "personal" and new_mode != "personal"


@router.get("")
async def list_servers(
    user: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    """Admin: every server. Moderator: only their own single-subject rows."""
    rows = await mcp_servers.list_all_rows(db)
    if user.get("role") == "moderator":
        rows = [r for r in rows if _is_own_group_server(r, user)]
    return await _with_creator_names(db, rows)


@router.post("", status_code=201)
async def create_server(
    body: McpServerCreate,
    user: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    subjects = await _validate_subjects(db, body.subjects)
    if user.get("role") == "moderator" and subjects != [_own_mod_subject(user)]:
        raise HTTPException(
            status_code=403,
            detail="A moderator may create servers for their own group only",
        )
    try:
        fields = mcp_servers.validate_definition(
            name=body.name, title=body.title, url=body.url,
            auth_mode=body.auth_mode, auth_scheme=body.auth_scheme,
            help_url=body.help_url,
        )
        await mcp_servers.check_egress(fields["host"])
        if body.shared_key is not None and fields["auth_mode"] != "shared":
            raise mcp_servers.McpServerError(
                "shared_key is only valid for a shared-key server",
            )
        key_sets = _shared_key_sets(fields, body.shared_key)
    except mcp_servers.McpServerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row = await mcp_servers.create_row(db, {
        "name": fields["name"],
        "title": fields["title"],
        "url": fields["url"],
        "auth_mode": fields["auth_mode"],
        "auth_scheme": fields["auth_scheme"],
        "help_url": fields["help_url"],
        **key_sets,
        "subjects": subjects,
        "created_by": user["user_id"],
        "enabled": body.enabled,
        "disabled_by": [],
    })
    if row is None:
        raise HTTPException(status_code=409, detail="A server with this name already exists")
    log_security("mcp_server_created", user_id=user["user_id"], server=fields["name"])
    return (await _with_creator_names(db, [row]))[0]


def _patch_column_sets(
    body: McpServerUpdate, row: dict, fields: dict, subjects: list[str] | None,
) -> tuple[dict, bool]:
    """(column updates, purge_personal_rows) of one PATCH beyond the plain
    merged fields: mode change, the shared key, subjects, enabled. Raises
    HTTPException; callers owe the collision check, the write and the purge."""
    if body.shared_key is not None and fields["auth_mode"] != "shared":
        raise HTTPException(
            status_code=422,
            detail="shared_key is only valid for a shared-key server",
        )
    mode_sets, purge_personal = _mode_change_sets(fields, row)
    try:
        key_sets = _shared_key_sets(fields, body.shared_key)
    except mcp_servers.McpServerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    sets: dict = {
        k: fields[k] for k in ("name", "title", "url", "auth_mode", "auth_scheme", "help_url")
    } | mode_sets | key_sets
    if subjects is not None:
        sets["subjects"] = subjects
    if body.enabled is not None:
        sets["enabled"] = body.enabled
    return sets, purge_personal


@router.patch("/{server_id}")
async def update_server(
    server_id: str,
    body: McpServerUpdate,
    user: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    row = await _scoped_row(db, user, server_id)
    subjects: list[str] | None = None
    if body.subjects is not None:
        subjects = await _validate_subjects(db, body.subjects)
        if user.get("role") == "moderator" and subjects != [_own_mod_subject(user)]:
            # Also covers "moderator strips the mod: subject from their own".
            raise HTTPException(
                status_code=403,
                detail="A moderator may not change a server's subjects",
            )
    try:
        fields = await _validated_fields(db, body, row)
    except mcp_servers.McpServerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    sets, purge_personal = _patch_column_sets(body, row, fields, subjects)
    collision = await db.query(
        "SELECT id FROM mcp_servers WHERE name = $n AND id != type::record('mcp_servers', $id)",
        {"n": sets["name"], "id": server_id},
    )
    if collision:
        raise HTTPException(status_code=409, detail="A server with this name already exists")
    await mcp_servers.update_row(db, server_id, sets)
    # INVARIANT(data-loss): every user's keys are purged only after the row write.
    # Why: a refused PATCH (409 on the name) must leave the keys in place.
    if purge_personal:
        await user_secrets.delete_all_server_rows(db, server_id)
        log_security(
            "mcp_server_keys_purged", user_id=user["user_id"],
            server=sets["name"],
        )
    log_security("mcp_server_updated", user_id=user["user_id"], server=sets["name"])
    updated = await mcp_servers.get_row(db, server_id)
    return (await _with_creator_names(db, [updated or row]))[0]


@router.post("/{server_id}/check")
async def check_server(
    server_id: str,
    user: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    """The admin/moderator connection check for `shared` and `none` servers
    (a personal server is each user's to check, from the cabinet)."""
    row = await _scoped_row(db, user, server_id)
    try:
        headers = await mcp_servers.checkable_headers(db, row, user=None)
        return await mcp_servers.check_connection(row.get("url") or "", headers)
    except mcp_servers.McpServerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{server_id}")
async def delete_server(
    server_id: str,
    user: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    """Hard delete: the server and every user's key rows for it (an
    orphaned ciphertext row is a credential left in the database)."""
    row = await _scoped_row(db, user, server_id)
    await mcp_servers.delete_row(db, server_id)
    log_security("mcp_server_deleted", user_id=user["user_id"], server=row.get("name"))
    return {"success": True}
