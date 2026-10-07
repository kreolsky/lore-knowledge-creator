"""Auth routes — login, logout, current user."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from password import verify_secret
from rate_limit import (
    check_login_device_rate_limit,
    check_login_email_rate_limit,
    reset_login_device_rate_limit,
    reset_login_email_rate_limit,
)
from security_log import log_security
from surrealdb import AsyncSurreal

# NOTE: `bump_token_version` lives in the top-level auth module (backend/auth.py), not this
# routes/auth.py module. The bare `from auth import ...` resolves to the top-level package.
from auth import (
    bump_token_version,
    get_current_user,
    read_device_nonce,
    set_device_cookie,
    set_session_cookie,
)
from db import extract_id, get_db
from models import LoginRequest, capability_flags

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/auth/login")
async def login(body: LoginRequest, request: Request, response: Response, db: AsyncSurreal = Depends(get_db)):
    """Authenticate user and set httpOnly session cookie."""
    client_ip = request.client.host if request.client else "unknown"
    # INVARIANT(security): a client with a valid device cookie for this account is
    # throttled only on its own counter; every other client shares the account's.
    # Why: strangers' failed guesses must never lock the owner's known device out.
    nonce = read_device_nonce(request, body.email)
    allowed = (
        await check_login_device_rate_limit(nonce) if nonce
        else await check_login_email_rate_limit(body.email)
    )
    if not allowed:
        log_security("login_rate_limited", ip=client_ip, tier="device" if nonce else "email")
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    try:
        rows = await db.query(
            "SELECT * FROM users WHERE email = $e AND deleted_at IS NONE LIMIT 1",
            {"e": body.email},
        )
    except Exception:
        logger.error("DB unavailable during login")
        return JSONResponse(status_code=503, content={"detail": "Service temporarily unavailable"})
    user = rows[0] if rows else None
    if not user or not user.get("password_hash"):
        log_security("login_failed", ip=client_ip, reason="unknown_email")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_secret(body.password, user["password_hash"]):
        log_security("login_failed", user_id=extract_id(user["id"]), ip=client_ip, reason="wrong_password")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # A success resets only the counter this attempt was judged on, then issues
    # a fresh device nonce: a copied cookie stops matching after this sign-in.
    if nonce:
        await reset_login_device_rate_limit(nonce)
    else:
        await reset_login_email_rate_limit(body.email)
    user_id = extract_id(user["id"])
    tv = user.get("token_version") or 0
    payload = {"user_id": user_id, "name": user["name"], "email": user.get("email", ""), "role": user["role"]}
    # ARCH: capability flags ride the login payload too (same shape as /me) —
    # the UI's gear gate reads can_manage_users straight after login, before
    # any /me refresh, and must never compare role strings in TSX.
    payload.update(capability_flags(user["role"]))
    set_session_cookie(request, response, user_id, user["name"], user.get("email", ""), user["role"], token_version=tv)
    set_device_cookie(request, response, body.email)
    log_security("login_success", user_id=user_id, ip=client_ip)
    return payload


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    """Clear the session cookie and invalidate the user's token version server-side.

    # INVARIANT(security): logout must be server-enforced, not cookie-only. Why: deleting only the
    # client cookie leaves a stolen cookie valid server-side for up to JWT TTL (14 days) +
    # the 60s token_version cache. bump_token_version() fans the invalidation to every
    # replica via the event_bus, so a reused stolen cookie is rejected on the next request.
    """
    cookie = request.cookies.get("lore_session")
    if cookie:
        try:
            user = await get_current_user(request=request, lore_session=cookie)
            await bump_token_version(user["user_id"])
        except HTTPException:
            # Expired/invalid cookie — nothing to invalidate, still clear client-side.
            pass
        except Exception:
            logger.warning("Failed to bump token version on logout", exc_info=True)
    # WHY the device cookie stays: it marks a device, not a session.
    response.delete_cookie(key="lore_session", samesite="lax")
    return {"success": True}


@router.get("/api/auth/me")
async def me(request: Request, response: Response, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Return current authenticated user info with has_pin flag.

    Sliding window: re-issues the session cookie on every call, pushing
    the expiration 14 days from now so active users are never logged out.
    """
    try:
        rows = await db.query(
            "SELECT pin_hash, token_version, role FROM type::record('users', $id)",
            {"id": user["user_id"]},
        )
        db_user = rows[0] if rows else {}
        user["has_pin"] = bool(db_user and db_user.get("pin_hash"))
        tv = (db_user.get("token_version") or 0) if db_user else 0
        # ARCH: /me answers with the DB role (fresh even when the JWT claim is
        # stale — a role flip takes effect without re-login), and with capability
        # flags so the frontend never compares role strings in TSX.
        if db_user and db_user.get("role"):
            user["role"] = db_user["role"]
        user.update(capability_flags(user.get("role")))
    except Exception:
        logger.error("DB unavailable during /me for user %s", user["user_id"])
        return JSONResponse(status_code=503, content={"detail": "Service temporarily unavailable"})
    set_session_cookie(request, response, user["user_id"], user["name"], user.get("email", ""), user.get("role", "user"), token_version=tv)
    return user
