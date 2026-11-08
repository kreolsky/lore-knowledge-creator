"""Cabinet routes — user self-service profile management.

ARCH: Cabinet endpoints let authenticated users manage their own profile
(name, email, password, PIN). Email/password changes bump token_version to
end every OTHER session (see INVARIANT(security) in
auth._invalidate_token_version_cache) and re-issue the caller's JWT cookie, so
the current tab stays signed in without re-login while a stolen cookie dies.
"""

import re

import mcp_servers
import user_secrets
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from password import hash_secret, verify_secret
from rate_limit import check_pin_rate_limit, check_rate_limit, reset_rate_limit
from security_log import log_security
from surrealdb import AsyncSurreal

from auth import bump_token_version, get_current_user, set_session_cookie
from db import extract_id, fetch_one, get_db, serialize_record
from models import (
    CreateSecretRequest,
    McpServerKeyValue,
    McpServerToggle,
    UpdateProfileEmail,
    UpdateProfileName,
    UpdateProfilePassword,
    UpdateProfilePin,
    UpdateSecretRequest,
    VerifyPinRequest,
)

router = APIRouter()

_PIN_RE = re.compile(r"^\d{4}$")



@router.patch("/api/cabinet/name")
async def change_name(
    body: UpdateProfileName,
    request: Request,
    response: Response,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Change display name. No confirmation needed."""
    rows = await db.query(
        "UPDATE type::record('users', $id) SET name = $name, updated_at = time::now() RETURN AFTER",
        {"id": user["user_id"], "name": body.name},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="User not found")
    set_session_cookie(request, response, user["user_id"], body.name, user.get("email", ""), user["role"], token_version=user.get("token_version") or 0)
    return serialize_record(rows[0], "user_id")


@router.patch("/api/cabinet/email")
async def change_email(
    body: UpdateProfileEmail,
    request: Request,
    response: Response,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Change email. Requires current password for confirmation."""
    if not await check_rate_limit(f"cabinet-email:{user['user_id']}"):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    # Verify current password
    rows = await db.query(
        "SELECT password_hash FROM type::record('users', $id)",
        {"id": user["user_id"]},
    )
    if not rows or not rows[0]:
        raise HTTPException(status_code=404, detail="User not found")
    if not verify_secret(body.current_password, rows[0]["password_hash"]):
        raise HTTPException(status_code=403, detail="Wrong password")
    # Check email uniqueness
    existing = await db.query(
        "SELECT id FROM users WHERE email = $e AND id != type::record('users', $uid) AND deleted_at IS NONE LIMIT 1",
        {"e": body.email, "uid": user["user_id"]},
    )
    if existing:
        raise HTTPException(status_code=409, detail="Email already in use")
    # Update
    updated = await db.query(
        "UPDATE type::record('users', $id) SET email = $email, updated_at = time::now() RETURN AFTER",
        {"id": user["user_id"], "email": body.email},
    )
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    # An email change is a credential change (the login identifier): end every
    # OTHER session and re-issue the caller's cookie on the new token_version.
    tv = await bump_token_version(user["user_id"])
    set_session_cookie(request, response, user["user_id"], user["name"], body.email, user["role"], token_version=tv)
    log_security("email_changed", user_id=user["user_id"])
    return serialize_record(updated[0], "user_id")


@router.patch("/api/cabinet/password")
async def change_password(
    body: UpdateProfilePassword,
    request: Request,
    response: Response,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Change password. Requires current password for confirmation.

    A password change ends every OTHER session (bump_token_version) and
    re-issues the caller's cookie on the new token_version, so the current tab
    stays signed in."""
    if not await check_rate_limit(f"cabinet-pwd:{user['user_id']}"):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    rows = await db.query(
        "SELECT password_hash FROM type::record('users', $id)",
        {"id": user["user_id"]},
    )
    if not rows or not rows[0]:
        raise HTTPException(status_code=404, detail="User not found")
    if not verify_secret(body.current_password, rows[0]["password_hash"]):
        raise HTTPException(status_code=403, detail="Wrong password")
    new_hash = hash_secret(body.new_password)
    await db.query(
        "UPDATE type::record('users', $id) SET password_hash = $ph, updated_at = time::now()",
        {"id": user["user_id"], "ph": new_hash},
    )
    tv = await bump_token_version(user["user_id"])
    set_session_cookie(request, response, user["user_id"], user["name"], user.get("email", ""), user["role"], token_version=tv)
    log_security("password_changed", user_id=user["user_id"])
    return {"success": True}


@router.patch("/api/cabinet/pin")
async def change_pin(
    body: UpdateProfilePin,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Set, change, or remove 4-digit PIN."""
    # INVARIANT(security): setting/changing/removing the PIN does NOT ask for the
    # current one — even while PIN-locked pages exist (SYSTEM: page-lock).
    # Why: operator decision — the cabinet (behind the login) is the deliberate
    # unconfirmed reset path; the page lock guards against casual eyes, not a
    # person at an unlocked session.
    if body.pin is not None and not _PIN_RE.match(body.pin):
        raise HTTPException(status_code=422, detail="PIN must be exactly 4 digits")
    pin_hash = hash_secret(body.pin) if body.pin else None
    await db.query(
        "UPDATE type::record('users', $id) SET pin_hash = $ph, updated_at = time::now()",
        {"id": user["user_id"], "ph": pin_hash},
    )
    log_security("pin_changed", user_id=user["user_id"], action="set" if body.pin else "removed")
    return {"has_pin": body.pin is not None}


@router.post("/api/cabinet/verify-pin")
async def verify_pin(
    body: VerifyPinRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Verify PIN for lock screen unlock. Rate-limited per user."""
    rate_key = f"pin:{user['user_id']}"
    if not await check_pin_rate_limit(rate_key):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    rows = await db.query(
        "SELECT pin_hash FROM type::record('users', $id)",
        {"id": user["user_id"]},
    )
    if not rows or not rows[0] or not rows[0].get("pin_hash"):
        return {"valid": False}
    valid = verify_secret(body.pin, rows[0]["pin_hash"])
    if valid:
        await reset_rate_limit(rate_key)
    else:
        log_security("pin_verify_failed", user_id=user["user_id"])
    return {"valid": valid}


async def _load_pin_hash(db: AsyncSurreal, user_id: str) -> str | None:
    """The caller's stored pin_hash, or None (no PIN set / user gone)."""
    rows = await db.query(
        "SELECT pin_hash FROM type::record('users', $id)",
        {"id": user_id},
    )
    if not rows or not rows[0]:
        return None
    return rows[0].get("pin_hash")


async def _load_locked_doc_ids(db: AsyncSurreal, user_id: str) -> list[str]:
    """The caller's locked set as a list (NONE = empty)."""
    rows = await db.query(
        "SELECT locked_doc_ids FROM type::record('users', $id)",
        {"id": user_id},
    )
    if not rows or not rows[0]:
        return []
    return list(rows[0].get("locked_doc_ids") or [])


async def _save_locked_doc_ids(db: AsyncSurreal, user_id: str, ids: list[str]) -> list[str]:
    """Persist the locked set (deduped, order-stable) and return it."""
    ids = list(dict.fromkeys(ids))
    await db.query(
        "UPDATE type::record('users', $id) SET locked_doc_ids = $ids, updated_at = time::now()",
        {"id": user_id, "ids": ids},
    )
    return ids


# ─── Page PIN lock (SYSTEM: page-lock, backend/page_lock.py) ─────────────────
# The lock is PERSONAL (stored on the user) and per page, NOT per subtree:
# children of a locked page are unaffected. Locking requires an account PIN;
# unlocking (un-marking) re-verifies it through the SAME verify_secret +
# check_pin_rate_limit pair as verify-pin — the account PIN is the only secret.


@router.post("/api/cabinet/locked-docs/{doc_id}")
async def lock_document(
    doc_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Add a page to the caller's PIN-locked set. Requires an account PIN and
    per-doc access; returns the whole set."""
    if not await _load_pin_hash(db, user["user_id"]):
        raise HTTPException(
            status_code=422, detail="Set a PIN in the cabinet first",
        )
    from access import get_document_access

    doc = await fetch_one("documents", doc_id)
    if not doc or doc.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Document not found")
    if not await get_document_access(doc_id, user):
        raise HTTPException(status_code=403, detail="No access to this document")
    ids = await _load_locked_doc_ids(db, user["user_id"])
    if doc_id not in ids:
        ids.append(doc_id)
        ids = await _save_locked_doc_ids(db, user["user_id"], ids)
    log_security("page_locked", user_id=user["user_id"], document_id=doc_id)
    return {"locked_doc_ids": ids}


@router.post("/api/cabinet/locked-docs/{doc_id}/remove")
async def unlock_document(
    doc_id: str,
    body: VerifyPinRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Remove a page from the caller's PIN-locked set. The body's PIN is
    verified with the same rate limiter as verify-pin (shared `pin:` budget —
    brute-forcing this endpoint is brute-forcing the PIN)."""
    rate_key = f"pin:{user['user_id']}"
    if not await check_pin_rate_limit(rate_key):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    pin_hash = await _load_pin_hash(db, user["user_id"])
    if not pin_hash or not verify_secret(body.pin, pin_hash):
        log_security("pin_verify_failed", user_id=user["user_id"])
        raise HTTPException(status_code=403, detail="Wrong PIN")
    await reset_rate_limit(rate_key)
    ids = await _load_locked_doc_ids(db, user["user_id"])
    if doc_id in ids:
        ids = await _save_locked_doc_ids(
            db, user["user_id"], [i for i in ids if i != doc_id],
        )
    log_security("page_unlocked", user_id=user["user_id"], document_id=doc_id)
    return {"locked_doc_ids": ids}


# ─── Personal secrets (SYSTEM: user-secrets, backend/user_secrets.py) ───────
# A user's personal API keys: name, description, the hosts the key is bound
# to, and the encrypted value. Only these routes (a cookie session —
# get_current_user decodes the JWT cookie and nothing else) write a row; no
# tool ever can.

def _secret_update_sets(row: dict, body: UpdateSecretRequest) -> dict:
    """The validated column updates of one PATCH.

    # INVARIANT(security): moving a key to another host requires entering the
    # key again.
    # Why: a stolen session must not be able to rebind a stored key to its own
    # server and spend it through the agent without ever knowing the value.
    """
    sets: dict = {}
    if body.hosts is not None:
        sets["hosts"] = user_secrets.validate_hosts(body.hosts)
        if body.value is None and user_secrets.hosts_changed(row, sets["hosts"]):
            raise user_secrets.SecretValidationError(
                "enter the key again to change its hosts",
            )
    if body.description is not None:
        sets["description"] = body.description
    if body.header_name is not None:
        sets["header_name"] = user_secrets.normalize_header_name(body.header_name)
    if body.header_template is not None:
        sets["header_template"] = user_secrets.normalize_header_template(body.header_template)
    if body.query_param is not None:
        sets["query_param"] = user_secrets.normalize_query_param(body.query_param)
    if body.enabled is not None:
        sets["enabled"] = body.enabled
    if body.value is not None:
        sets["ciphertext"] = user_secrets.encrypt_value(
            user_secrets.normalize_value(body.value),
        )
    return sets


@router.get("/api/cabinet/secrets")
async def list_secrets(
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """The caller's personal keys — metadata and is_set, NEVER the value.

    # INVARIANT(security): a user secret never enters a REST response.
    # Why: every response is model-visible (chat frames relay tool/REST
    # payloads) or attacker-visible (a leaked session reads the cabinet); the
    # value lives only in the Fernet ciphertext and the http_request handler.
    # An undecryptable row (key file lost in a restore) counts as not set —
    # the UI shows "re-enter key", never an empty secret.
    """
    rows = await user_secrets.list_rows(db, user["user_id"])
    return {"secrets": [user_secrets.public_view(r) for r in rows]}


@router.post("/api/cabinet/secrets")
async def create_secret(
    body: CreateSecretRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Store one personal key (encrypted) bound to its hosts."""
    try:
        fields = (
            user_secrets.validate_name(body.name),
            user_secrets.normalize_value(body.value),
            body.description,
            user_secrets.validate_hosts(body.hosts),
            user_secrets.normalize_header_name(body.header_name),
            user_secrets.normalize_header_template(body.header_template),
            user_secrets.normalize_query_param(body.query_param),
        )
    except user_secrets.SecretValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    row = await user_secrets.create_row(db, user["user_id"], *fields)
    if row is None:
        raise HTTPException(status_code=409, detail="A secret with this name already exists")
    log_security("user_secret_created", user_id=user["user_id"])
    return user_secrets.public_view(row)


@router.patch("/api/cabinet/secrets/{name}")
async def update_secret(
    name: str,
    body: UpdateSecretRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Update one personal key. A body without `value` keeps the stored
    ciphertext (the UI never re-sends it); one with `value` re-enters the key."""
    row = await user_secrets.get_row(db, user["user_id"], name)
    if not row:
        raise HTTPException(status_code=404, detail="Secret not found")
    try:
        sets = _secret_update_sets(row, body)
    except user_secrets.SecretValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await user_secrets.update_row(db, extract_id(row["id"]), sets)
    updated = await user_secrets.get_row(db, user["user_id"], name)
    log_security("user_secret_updated", user_id=user["user_id"])
    return user_secrets.public_view(updated or row)


@router.delete("/api/cabinet/secrets/{name}")
async def delete_secret(
    name: str,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Delete one personal key (the ciphertext row — there is no soft delete)."""
    row = await user_secrets.get_row(db, user["user_id"], name)
    if not row:
        raise HTTPException(status_code=404, detail="Secret not found")
    await user_secrets.delete_row(db, extract_id(row["id"]))
    log_security("user_secret_deleted", user_id=user["user_id"])
    return {"success": True}


# ─── Personal MCP servers (SYSTEM: mcp-servers, backend/mcp_servers.py) ──────
# The cabinet half: the servers visible to the caller (plus the ones they
# still hold a key for but can no longer see — unavailable, delete-only),
# the write-only key field ("re-enter key"), the per-user toggle, the key
# DELETE and the connection check. Only these routes (a cookie session)
# write a key row; the turn payload's renderer is the only plaintext reader.


async def _visible_mcp_server(db: AsyncSurreal, user: dict, server_id: str) -> dict:
    row = await mcp_servers.visible_row(db, user, server_id)
    if not row:
        # Same posture as require_moderator_scope: an invisible server does
        # not exist — a disabled or revoked server is not enumerable here.
        raise HTTPException(status_code=404, detail="Server not found")
    return row


async def _cabinet_entry(db: AsyncSurreal, user: dict, server_id: str) -> dict | None:
    return next(
        (s for s in await mcp_servers.cabinet_view(db, user) if s["id"] == server_id),
        None,
    )


@router.get("/api/cabinet/mcp")
async def list_mcp_servers(
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """The caller's MCP servers: host line, creator, the write-only key
    state, the per-user toggle, unavailable cards for keys stranded on
    servers they can no longer see.

    # INVARIANT(security): a user secret never enters a REST response.
    # Why: same exposure as list_secrets — the value lives only in the
    # Fernet ciphertext and the turn payload's renderer.
    """
    return {"servers": await mcp_servers.cabinet_view(db, user)}


@router.put("/api/cabinet/mcp/{server_id}/secrets")
async def put_mcp_server_secrets(
    server_id: str,
    body: McpServerKeyValue,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Store the caller's key for the server. The row's hosts binding is the
    server's CURRENT host (the user cannot pick a destination — same
    exfiltration guard as the plain secrets), a pasted `<scheme> ` prefix is
    stripped, and a saved key revives a declined toggle (the user clearly
    wants the server)."""
    row = await _visible_mcp_server(db, user, server_id)
    try:
        host, port = mcp_servers.parse_server_url(row.get("url") or "")
    except mcp_servers.McpServerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        value = mcp_servers.normalize_key(
            body.value, row.get("auth_scheme") or "",
        )
    except mcp_servers.McpServerError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await user_secrets.put_server_key(
        db, user["user_id"],
        server_id=server_id, host=host, port=port, value=value,
    )
    await mcp_servers.set_user_disabled(db, server_id, user["user_id"], False)
    log_security("mcp_server_key_saved", user_id=user["user_id"], server=row.get("name"))
    return {"server": await _cabinet_entry(db, user, server_id)}


@router.patch("/api/cabinet/mcp/{server_id}")
async def toggle_mcp_server(
    server_id: str,
    body: McpServerToggle,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """The per-user toggle, EVERY auth mode: adds/removes the caller in the
    server row's disabled_by — even a keyless server's tool schemas ride
    every request, so it must be declinable too."""
    row = await _visible_mcp_server(db, user, server_id)
    await mcp_servers.set_user_disabled(
        db, server_id, user["user_id"], disabled=not body.enabled,
    )
    log_security(
        "mcp_server_toggled", user_id=user["user_id"],
        server=row.get("name"), enabled=body.enabled,
    )
    return {"server": await _cabinet_entry(db, user, server_id)}


@router.post("/api/cabinet/mcp/{server_id}/check")
async def check_mcp_server(
    server_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """The connection check with the caller's OWN render: initialize +
    tools/list with the key they would ride the turn with, classified
    (ok/unauthorized/refused/unreachable/protocol) — a wrong key must not
    look like success."""
    row = await _visible_mcp_server(db, user, server_id)
    try:
        headers = await mcp_servers.checkable_headers(db, row, user=user)
        return await mcp_servers.check_connection(row.get("url") or "", headers)
    except mcp_servers.McpServerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/api/cabinet/mcp/{server_id}/secrets")
async def delete_mcp_server_secrets(
    server_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Delete the caller's key rows for the server (the ciphertext rows —
    there is no soft delete). No visibility requirement: it deletes only the
    caller's OWN rows, and a key stranded on a server the admin disabled or
    revoked must be deletable — an orphaned ciphertext is a credential left
    in the database."""
    row = await mcp_servers.get_row(db, server_id)
    await user_secrets.delete_server_rows(db, user["user_id"], server_id)
    log_security(
        "mcp_server_key_deleted", user_id=user["user_id"],
        server=(row or {}).get("name") or "",
    )
    return {"success": True}
