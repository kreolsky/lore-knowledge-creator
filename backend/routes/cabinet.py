"""Cabinet routes — user self-service profile management.

ARCH: Cabinet endpoints let authenticated users manage their own profile
(name, email, password, PIN). Name/email changes re-issue the JWT cookie
so the frontend stays in sync without re-login.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from password import hash_secret, verify_secret
from rate_limit import check_pin_rate_limit, check_rate_limit, reset_rate_limit
from security_log import log_security
from surrealdb import AsyncSurreal

from auth import get_current_user, set_session_cookie
from db import get_db, serialize_record
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
    set_session_cookie(request, response, user["user_id"], user["name"], body.email, user["role"], token_version=user.get("token_version") or 0)
    log_security("email_changed", user_id=user["user_id"])
    return serialize_record(updated[0], "user_id")


@router.patch("/api/cabinet/password")
async def change_password(
    body: UpdateProfilePassword,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Change password. Requires current password for confirmation."""
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
    log_security("password_changed", user_id=user["user_id"])
    return {"success": True}


@router.patch("/api/cabinet/pin")
async def change_pin(
    body: UpdateProfilePin,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Set, change, or remove 4-digit PIN."""
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
