"""Authentication — JWT encode/decode, cookie management, WS token validation."""
# ARCH: Cookie-only auth — no query-param tokens.
# ARCH: token_version in JWT — bump user.token_version in DB to invalidate all sessions.
# SYSTEM: auth — JWT cookie-based auth with token versioning for session invalidation

import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Cookie, Depends, HTTPException, Request, Response, WebSocket
from security_log import log_security

import event_bus

# `is_instance_admin` hoisted module-level (no cycle: access never imports auth;
# nothing monkeypatches it). db.fetch_one stays IN-FUNCTION and event_bus.emit is
# read as a module attribute at the call — both are test seams
# (tests/backend/db_leak_guard.py: call-time resolution re-reads the module
# attribute a monkeypatch.setattr("db.fetch_one", ...) has rebound).
from access import is_instance_admin
from config import ALGORITHM, COOKIE_MAX_AGE, SECRET_KEY
from models.auth import USER_MANAGER_ROLES

# token_version is read on EVERY request/WS connect; a short TTL cache keeps auth
# latency off the DB hot path. {user_id: (token_version, role, expires_at_monotonic)}.
# role rides the same row read so a role change is authoritative on the target's
# very next request WITHOUT a logout (see refresh_user_role_cache).
_TOKEN_VERSION_TTL_S = 60
_token_version_cache: dict[str, tuple[int, str | None, float]] = {}


def _invalidate_token_version_cache(user_id: str) -> None:
    # INVARIANT(security): every SESSION-invalidation MUST go through
    # bump_token_version(); every role change MUST go through
    # refresh_user_role_cache(). Why: this pop only clears the calling process's
    # cache; with multiple replicas (web + workers) a local pop alone would let
    # other processes honor a revoked session (or a stale role) for up to
    # _TOKEN_VERSION_TTL_S. Both fan the invalidation out via the event_bus so
    # every replica drops its cache.
    _token_version_cache.pop(user_id, None)


async def bump_token_version(user_id: str) -> int:
    """Invalidate all of a user's sessions by incrementing token_version.

    Bumps the DB value, drops the local cache *synchronously* (emit is
    fire-and-forget — the local invalidation cannot wait on it), then fans the
    invalidation out to other replicas via the event_bus (Redis evt: channel).
    Returns the new token_version, so a caller that keeps its own session alive
    re-issues its cookie on it. Raises on a missing user row.
    """
    from db import get_db
    db = await get_db()
    rows = await db.query(
        "UPDATE type::record('users', $id) SET token_version = (token_version ?? 0) + 1 "
        "RETURN AFTER",
        {"id": user_id},
    )
    _invalidate_token_version_cache(user_id)
    await event_bus.emit("token_version_bumped", user_id=user_id)
    return rows[0]["token_version"]


def on_token_version_bumped(user_id: str) -> None:
    """event_bus subscriber: drop the local cache when another replica bumps."""
    _invalidate_token_version_cache(user_id)


async def refresh_user_role_cache(user_id: str) -> None:
    """Make a role change take effect without logging the target out.

    Drops the local cache synchronously and fans the SAME token_version_bumped
    event out to other replicas — but does NOT touch token_version: the target's
    cookies stay valid, and their next request refetches the row (role overlay in
    get_current_user) authorized under the new role.
    """
    _invalidate_token_version_cache(user_id)
    await event_bus.emit("token_version_bumped", user_id=user_id)


def _browser_scheme_is_https(request: Request) -> bool:
    """Whether the BROWSER reached Lore over HTTPS.

    The first hop of X-Forwarded-Proto (set by the operator's TLS proxy and
    passed on by the bundled nginx), else the request's own scheme.
    WHY trust the header: a client spoofing it only changes the flag on its
    own cookie.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded.split(",")[0].strip().lower() or request.url.scheme
    return scheme == "https"


def set_session_cookie(
    request: Request, response: Response, user_id: str, name: str, email: str, role: str,
    token_version: int = 0,
) -> None:
    """Encode a JWT and set it as the httpOnly session cookie."""
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "user_id": user_id, "name": name, "email": email, "role": role,
            "token_version": token_version,
            "exp": now + timedelta(seconds=COOKIE_MAX_AGE),
            "iat": now,
        },
        SECRET_KEY, algorithm=ALGORITHM,
    )
    response.set_cookie(
        key="lore_session", value=token,
        httponly=True, samesite="lax",
        # INVARIANT(security): Secure follows the browser's scheme, never a setting.
        # Why: a Secure cookie over plain HTTP never comes back (sign-in does not
        # stick), and a non-Secure one over HTTPS leaks on a downgrade — a
        # one-file install must get both right with no operator action.
        secure=_browser_scheme_is_https(request), max_age=COOKIE_MAX_AGE,
    )


DEVICE_COOKIE = "lore_device"
_DEVICE_COOKIE_MAX_AGE = 60 * 60 * 24 * 180  # 180 days


def _device_subject(email: str) -> str:
    return email.strip().lower()


def set_device_cookie(request: Request, response: Response, email: str) -> None:
    """Mark this browser as a known device for `email`, with a fresh nonce.

    The nonce keys the device's own login-throttle counter; a new one on every
    success means a copied cookie stops matching after the owner's next sign-in.
    """
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "typ": "device", "sub": _device_subject(email), "nonce": uuid.uuid4().hex,
            "iat": now, "exp": now + timedelta(seconds=_DEVICE_COOKIE_MAX_AGE),
        },
        SECRET_KEY, algorithm=ALGORITHM,
    )
    response.set_cookie(
        key=DEVICE_COOKIE, value=token,
        httponly=True, samesite="lax", path="/api/auth",
        # Same Secure rule as the session cookie: see the INVARIANT in set_session_cookie.
        secure=_browser_scheme_is_https(request), max_age=_DEVICE_COOKIE_MAX_AGE,
    )


def read_device_nonce(request: Request, email: str) -> str | None:
    """The device nonce, only for a valid unexpired device cookie bound to `email`.

    Anything else (absent, forged, expired, another account's) is None: an
    untrusted client, not an error.
    """
    token = request.cookies.get(DEVICE_COOKIE)
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.InvalidTokenError:
        return None
    if payload.get("typ") != "device" or payload.get("sub") != _device_subject(email):
        return None
    nonce = payload.get("nonce")
    return nonce if isinstance(nonce, str) and nonce else None


async def _verify_token_version(payload: dict, path: str = "") -> str | None:
    """Check JWT token_version against DB. Raises 401 if stale or missing.

    Returns the FRESH DB role for the user (None only if the row carries no
    role) so callers can overlay it onto the JWT payload — a role change must
    be authoritative on the next request, not on the next login.

    path is the originating request URL (HTTP or WS) — included in every
    log_security call here so prod diagnostics can correlate 401s to endpoints.
    """
    tv_jwt = payload.get("token_version")
    if tv_jwt is None:
        # H-8: Legacy tokens without token_version must be rejected
        log_security("legacy_token_no_version", user_id=payload.get("user_id"), path=path)
        raise HTTPException(status_code=401, detail="Session expired — please log in again")
    user_id = payload["user_id"]
    cached = _token_version_cache.get(user_id)
    if cached is not None and cached[2] > time.monotonic():
        tv_db, role_db = cached[0], cached[1]
    else:
        from db import fetch_one
        user = await fetch_one("users", user_id)
        if not user:
            log_security("user_not_found", user_id=user_id, path=path)
            raise HTTPException(status_code=401, detail="User not found")
        tv_db = user.get("token_version") or 0
        role_db = user.get("role")
        _token_version_cache[user_id] = (tv_db, role_db, time.monotonic() + _TOKEN_VERSION_TTL_S)
    if tv_db != tv_jwt:
        log_security("stale_token_version", user_id=user_id, path=path)
        raise HTTPException(status_code=401, detail="Session invalidated")
    return role_db


def _overlay_db_role(payload: dict, role_db: str | None) -> dict:
    """ARCH: role overlay — the DB role wins over the JWT claim so an admin's
    role flip (PATCH, which calls refresh_user_role_cache) authorizes the
    target's very next request without a logout or token_version bump.

    Shared by get_current_user (HTTP) and validate_ws_token (WS connect) —
    both paths must authorize under the same fresh role, else a demoted
    admin keeps admin access on every WS connect until re-login.
    """
    if role_db is not None and payload.get("role") != role_db:
        payload["role"] = role_db
    return payload


async def get_current_user(
    request: Request,
    lore_session: str | None = Cookie(default=None),
) -> dict:
    """FastAPI dependency: decode JWT cookie and return user payload.

    # ARCH: every 401 raise site emits a reason-tagged log_security event with the
    # request path, so spontaneous-logouts are diagnosable from the structured
    # security log alone (prod runs uvicorn --log-level warning, suppressing
    # access logs). Reasons: no_cookie / expired / invalid (+ stale/legacy/user_not_found
    # from _verify_token_version).
    """
    path = request.url.path
    if not lore_session:
        log_security("no_cookie", user_id=None, path=path)
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(lore_session, SECRET_KEY, algorithms=[ALGORITHM], options={"require": ["exp"]})
    except jwt.ExpiredSignatureError:
        # WHY: distinguish true cookie expiry from tampering — the spontaneous-logout
        # hypothesis predicts `expired` cadence matching the 14-day cookie boundary.
        log_security("expired", user_id=_peek_user_id(lore_session), path=path)
        raise HTTPException(status_code=401, detail="Session expired")
    except jwt.InvalidTokenError:
        log_security("invalid", user_id=None, path=path)
        raise HTTPException(status_code=401, detail="Invalid session")
    role_db = await _verify_token_version(payload, path=path)
    return _overlay_db_role(payload, role_db)


def _peek_user_id(token: str) -> str | None:
    """Best-effort decode of user_id from an expired JWT (no signature verify).

    Used only for diagnostic logging — never gates access.
    """
    try:
        return jwt.decode(token, options={"verify_signature": False}).get("user_id")
    except Exception:
        return None


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """FastAPI dependency: assert current user has admin role."""
    if not is_instance_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_user_manager_role(user: dict = Depends(get_current_user)) -> dict:
    """FastAPI dependency: admin | moderator — the role gate for the user list/create routes.

    NOT an instance-admin grant: a moderator passing this gate is still scoped
    to their group by require_user_manager / the list filter.
    """
    if user.get("role") not in USER_MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="User management access required")
    return user


async def require_user_manager(user_id: str, user: dict = Depends(get_current_user)) -> dict:
    """FastAPI dependency: the ONE guard for every per-user action (PATCH/DELETE,
    project-access).

    admin → any target; moderator → only a role='user' row with
    moderator_id == me; anything else → 404 — a moderator must not learn
    that a foreign user exists. `user_id` is matched from the route's path
    parameter.
    """
    if is_instance_admin(user):
        return user
    if user.get("role") == "moderator":
        from db import fetch_one
        target = await fetch_one("users", user_id)
        if target and target.get("role") == "user" and target.get("moderator_id") == user["user_id"]:
            return user
        raise HTTPException(status_code=404, detail="User not found")
    raise HTTPException(status_code=403, detail="User management access required")


async def validate_ws_token(ws: WebSocket) -> dict | None:
    """Validate WS token from cookie only. Returns user dict or None.

    C-1: Now async — verifies token_version against DB (same as HTTP auth).
    """
    token = ws.cookies.get("lore_session")
    if not token:
        return None
    path = ws.url.path
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"require": ["exp"]})
    except jwt.ExpiredSignatureError:
        # WHY: mirror get_current_user's expired/invalid split so WS-originated auth
        # failures are visible in lore.security for the spontaneous-logout diagnostics.
        log_security("expired", user_id=_peek_user_id(token), path=path)
        return None
    except jwt.InvalidTokenError:
        log_security("invalid", user_id=None, path=path)
        return None
    try:
        role_db = await _verify_token_version(payload, path=path)
    except HTTPException:
        return None
    return _overlay_db_role(payload, role_db)
