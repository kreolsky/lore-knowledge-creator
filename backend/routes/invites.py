"""Registration invite links — mint (admin | moderator) and public register.

# SYSTEM: registration-invites — single-use invite tokens behind /register/<token>
#
# ARCH: token = secrets.token_urlsafe(32), persisted ONLY as its sha256 (same
# posture as api_keys, backend/api_key_auth.py mint_token); the plaintext is
# returned exactly once, in the mint response. There is no registration without
# a token — the register routes are the only user-creation path reachable
# without a session, and they require a valid unused unexpired invite.
#
# ARCH: single-use is claimed with a conditional UPDATE (WHERE used_at IS NONE)
# BEFORE the user row is created — a concurrent second register on the same
# token claims nothing and reads 404. The email-duplicate 409 is checked BEFORE
# the claim, so a rejected attempt does not burn the invite; a failure AFTER the
# claim burns it (fail-closed — the safe direction for a single-use credential).
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from password import hash_secret
from rate_limit import check_rate_limit
from security_log import log_security
from surrealdb import AsyncSurreal

from auth import require_user_manager_role, set_session_cookie
from db import create_record, fetch_one, get_db
from models import CreateInvite, RegisterRequest
from routes.users import _flush_pending_invites

router = APIRouter()

INVITE_TTL_DAYS = 3


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _load_valid_invite(db: AsyncSurreal, token: str) -> dict:
    """Fetch the invite row or 404. Missing, used and expired read IDENTICALLY —
    a probe must not learn which tokens ever existed."""
    rows = await db.query(
        "SELECT * FROM registration_invites "
        "WHERE token_hash = $h AND used_at IS NONE AND expires_at > time::now() LIMIT 1",
        {"h": _hash_token(token)},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Invite not found or no longer valid")
    return rows[0]


def _client_rate_gate(client_ip: str | None) -> str:
    """Shared limiter key for the public register surface (the login tier)."""
    return client_ip or "unknown"


@router.post("/api/invites")
async def create_invite(
    body: CreateInvite,
    manager: dict = Depends(require_user_manager_role),
    db: AsyncSurreal = Depends(get_db),
):
    """Mint one single-use invite token. Moderator: the row is pinned to the own
    group (body's moderator_id ignored — same forcing as create_user_admin).
    Admin: optional moderator_id from the body, validated as a moderator uid.

    # ARCH: the mint returns the raw token, never a URL — the CLIENT composes
    # `${window.location.origin}/register/${token}` (AdminPage), the origin of
    # the page the admin is looking at and therefore reachable by the invitee.
    # A server-side origin guess (request.base_url, or a public-URL env
    # override — both removed) was wrong behind every proxy it was not
    # explicitly configured for.
    """
    if manager.get("role") == "moderator":
        moderator_id = manager["user_id"]
    else:
        moderator_id = body.moderator_id
        if moderator_id is not None:
            mod_row = await fetch_one("users", moderator_id)
            if not mod_row or mod_row.get("role") != "moderator":
                raise HTTPException(status_code=422, detail="moderator_id must point at a moderator")
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=INVITE_TTL_DAYS)
    await create_record("registration_invites", str(uuid4()), {
        "token_hash": _hash_token(token),
        "invited_by": manager["user_id"],
        "moderator_id": moderator_id,
        "expires_at": expires_at,
    })
    return {"token": token, "expires_at": expires_at}


@router.get("/api/register/{token}")
async def get_invite(token: str, request: Request, db: AsyncSurreal = Depends(get_db)):
    """Public: the register page's payload — the inviter's display name."""
    client_ip = request.client.host if request.client else None
    if not await check_rate_limit(_client_rate_gate(client_ip)):
        log_security("register_rate_limited", ip=client_ip)
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    invite = await _load_valid_invite(db, token)
    inviter = await fetch_one("users", invite["invited_by"])
    return {"inviter_name": (inviter or {}).get("name")}


async def _claim_single_use(db: AsyncSurreal, token_hash: str) -> None:
    """Atomically flip used_at; a lost race reads as an invalid invite (404)."""
    claimed = await db.query(
        "UPDATE registration_invites SET used_at = time::now() "
        "WHERE token_hash = $h AND used_at IS NONE RETURN AFTER",
        {"h": token_hash},
    )
    if not claimed:
        raise HTTPException(status_code=404, detail="Invite not found or no longer valid")


async def _create_registered_user(db: AsyncSurreal, invite: dict, body: RegisterRequest, token_hash: str) -> str:
    """Create the invite-bound role='user' row, stamp used_by, and apply any
    queued project invites for the email (same flush as create_user_admin).

    The invite's group pointer is re-validated at claim time: the moderator
    may have been demoted or soft-deleted between mint and register, and
    moderator_id is legal only on a live role='moderator' row (schema
    users.moderator_id ASSERT). The invite stays valid — the account lands
    ungrouped (what an admin's manual reassign produces), and created_by
    still records the inviter."""
    moderator_id = invite.get("moderator_id")
    if moderator_id is not None:
        mod = await fetch_one("users", moderator_id)
        if not mod or mod.get("role") != "moderator":
            moderator_id = None
    uid = str(uuid4())
    await create_record("users", uid, {
        "name": body.name,
        "email": body.email,
        "password_hash": hash_secret(body.password),
        "role": "user",
        "user_facts": "",
        "created_by": invite["invited_by"],
        "moderator_id": moderator_id,
    })
    await db.query(
        "UPDATE registration_invites SET used_by = $uid WHERE token_hash = $h",
        {"uid": uid, "h": token_hash},
    )
    await _flush_pending_invites(body.email, uid)
    return uid


@router.post("/api/register/{token}")
async def register_with_invite(
    token: str,
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: AsyncSurreal = Depends(get_db),
):
    """Public: consume the invite, create the bound role='user' account, apply
    queued project invites for the email, and log the new user in (cookie).

    # INVARIANT(security): no rate-limit reset on success (unlike login). Why:
    # a successful register does not prove a human the way a password match does
    # — each registration is bounded by an invite an admin/moderator minted, and
    # resetting the IP budget here would make that bound the ONLY bound.
    """
    client_ip = request.client.host if request.client else None
    if not await check_rate_limit(_client_rate_gate(client_ip)):
        log_security("register_rate_limited", ip=client_ip)
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    invite = await _load_valid_invite(db, token)
    # Duplicate email BEFORE the single-use claim: a 409 must not burn the invite
    # (same check as create_user_admin).
    dup = await db.query(
        "SELECT id FROM users WHERE email = $e AND deleted_at IS NONE LIMIT 1",
        {"e": body.email},
    )
    if dup:
        raise HTTPException(status_code=409, detail="Email already in use")
    token_hash = _hash_token(token)
    await _claim_single_use(db, token_hash)
    uid = await _create_registered_user(db, invite, body, token_hash)
    set_session_cookie(response, uid, body.name, body.email, "user", token_version=0)
    log_security("register_success", user_id=uid, ip=client_ip)
    return {"user_id": uid, "name": body.name, "email": body.email, "role": "user"}
