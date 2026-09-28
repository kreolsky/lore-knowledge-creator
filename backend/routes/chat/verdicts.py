"""Verdict REST surface — list a session's pending asks, publish the user's verdict.

# SYSTEM: chat-verdicts — the UI-facing half of mid-turn approval: the chat panel
# lists a session's still-asked calls (reload re-render) and posts the user's
# verdict (allow once / allow for the session / reject with text).

# ARCH: the ASK lives in
# the driver — dsh's user-approval service parks it (the audit pair
# approval/asked + approval/decided on the session log), and this surface
# RELAYS the user's answer across the driver seam. Our RBAC stays the answerer's gate:
# both endpoints verify the session belongs to the current user
# (`_require_session_access`) BEFORE anything reaches the driver, and the
# driver's X-Driver-Secret endpoints speak only to this backend — the verdict
# of a user who cannot read the session never crosses the seam.

# ARCH: the driver owns the answers' vocabulary: 200 (resolved / listed), 404
# (no parked ask for this call id in this session — the uniform refusal), 409
# (the OWNING session's ask expired past the driver's approval bound), 422
# (a malformed action / an allow_session without its tool name). This module
# maps those statuses one-to-one; a driver that cannot be reached is an
# explicit 502 — never a silent success (no-silent-degradation).
"""
from typing import Literal

import http_clients
import httpx
from driver.client import resolve_driver_line
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from rate_limit import check_verdict_rate_limit

from auth import get_current_user
from routes.chat._router import router
from routes.chat.sessions import _require_session_access

#: The driver-HTTP timeout: a verdict forward resolves a parked promise (fast);
#: the listing reads an in-process map. Generous only for a loaded event loop.
_VERDICT_FORWARD_TIMEOUT_S = 10.0


class VerdictRequest(BaseModel):
    call_id: str
    session_id: str
    action: Literal["allow_once", "allow_session", "reject"]
    # The tool the user approved on the card. For allow_session the DRIVER
    # cross-checks it against the parked ask's record and mints the grant from
    # the record — never from this body.
    tool_name: str | None = None
    reason: str | None = None


async def _require_driver():
    """The configured driver line or the explicit 503 — an unconfigured line
    cannot hold asks, so a verdict surface without it says so honestly."""
    line = await resolve_driver_line()
    if line is None or not line.secret:
        raise HTTPException(status_code=503, detail="agent driver not configured")
    return line


async def _driver_request(method: str, path: str, *, params=None, json_body=None):
    """One authenticated driver call. Returns the parsed JSON on 2xx; maps the
    driver's 404/409/422 onto HTTPException; anything else (including an
    unreachable driver) is the explicit 502 — a verdict the user believes was
    delivered but was not is the exact silent failure this surface refuses."""
    line = await _require_driver()
    try:
        client = http_clients.get_http_client(
            "driver", timeout=_VERDICT_FORWARD_TIMEOUT_S)
        resp = await client.request(
            method,
            f"{line.url}{path}",
            params=params,
            json=json_body,
            headers={"X-Driver-Secret": line.secret},
            timeout=_VERDICT_FORWARD_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"agent driver unreachable: {exc}",
        ) from exc
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code in (404, 409, 422):
        # The driver's own refusal vocabulary — surface it verbatim (the 409
        # detail `hold_expired` is the contract the frontend toasts on).
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)
    raise HTTPException(
        status_code=502,
        detail=f"agent driver verdict relay failed: {resp.status_code}",
    )


@router.get("/verdicts")
async def list_verdicts(
    session_id: str,
    user: dict = Depends(get_current_user),
):
    """The session's still-parked asks — the reload re-render path (the verdict-ask
    card is the live path; this is how a reconnecting chat panel rediscovers it)."""
    await _require_session_access(session_id, user)
    data = await _driver_request(
        "GET", "/approvals", params={"session_id": session_id},
    )
    return {"holds": data.get("holds", [])}


@router.post(
    "/verdicts",
    responses={409: {"description": "The parked ask expired before the verdict "
                                    "arrived (detail=hold_expired)"}},
)
async def post_verdict(
    body: VerdictRequest,
    user: dict = Depends(get_current_user),
):
    """Publish the user's verdict for one parked ask.

    The ask resolves as: allow_once → apply this call; allow_session →
    apply AND grant the tool for the session (driver-side, keyed by the parked
    ask's record); reject → return the user's words as the call's own result.
    The session ownership check here is the RBAC half of the answerer — the
    driver's endpoint trusts only this relay.
    """
    if not await check_verdict_rate_limit(user["user_id"]):
        raise HTTPException(status_code=429, detail="Too many verdict requests")
    await _require_session_access(body.session_id, user)
    await _driver_request(
        "POST",
        "/approvals",
        json_body={
            "call_id": body.call_id,
            "session_id": body.session_id,
            "action": body.action,
            "tool_name": body.tool_name,
            "reason": body.reason,
        },
    )
    return {"success": True}


async def resolve_session_asks(session_id: str, *, reason: str = "session_closed") -> None:
    """Reject every parked ask of a session (the session-delete path).

    Best-effort by contract (the caller soft-deletes first); a driver that
    cannot be reached still leaves every ask bounded by the driver's own
    approval cap, so an unreachable relay degrades to a later self-resolution,
    never an immortal card.
    """
    await _driver_request(
        "POST", "/approvals/resolve", json_body={"session_id": session_id, "reason": reason},
    )
