"""Cabinet routes — user self-service profile management.

ARCH: Cabinet endpoints let authenticated users manage their own profile
(name, email, password, PIN). Email/password changes bump token_version to
end every OTHER session (see INVARIANT(security) in
auth._invalidate_token_version_cache) and re-issue the caller's JWT cookie, so
the current tab stays signed in without re-login while a stolen cookie dies.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from password import hash_secret, verify_secret
from rate_limit import check_pin_rate_limit, check_rate_limit, reset_rate_limit
from security_log import log_security
from surrealdb import AsyncSurreal

from auth import bump_token_version, get_current_user, set_session_cookie
from db import fetch_one, get_db, serialize_record
from models import (
    UpdateProfileEmail,
    UpdateProfileName,
    UpdateProfilePassword,
    UpdateProfilePin,
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
