"""Agent request context — the Tool-API auth dependency and the caller's own chat session.

get_agent_context resolves a Bearer agent key to the owning user's ctx and reads
the dsh-driver correlation headers onto it; owned_session_row and
pinned_session_doc_id answer questions about the caller's OWN chat session. Service
code, not a route: routes/tool_api/* and agent_tools/routing both depend on it.

# ARCH: Auth(GAP-5): an agent-capable api_keys row ('agent' in
# capabilities; document_id = subtree root, '' = whole project for internal agent-minted
# keys). The key resolves to the OWNING user; every call runs
# get_document_access against THAT user — the agent can never exceed the user's rights.
# WHY: the agent's document access IS the invoking user's; there is no synthetic
# user with independent grants.
# Why: a separate agent identity could exceed the user's rights — a privilege-escalation hole.
"""
import secrets

from api_key_auth import authenticate_agent_token
from fastapi import Header, HTTPException

from db import fetch_one


async def driver_attested(driver_secret: str | None) -> bool:
    """True when a request carries OUR driver's shared secret.

    # INVARIANT(security): the X-Agent-Verdict marker is honored ONLY on a
    # driver-attested request. Why: the marker is a plain header on a surface
    # any agent-capable key can reach (external brains included), and it turns
    # a confirm cell into an auto-apply — without this check the caller
    # asserting "the user approved" IS the only evidence they have. The
    # driver secret is the one thing an external key holder does not have; the
    # is_system refusal (routes/tool_api/_common.py) stays as defence in depth.
    """
    if not isinstance(driver_secret, str) or not driver_secret:
        return False
    from driver.client import resolve_driver_line

    line = await resolve_driver_line()
    if line is None or not line.secret:
        return False
    return secrets.compare_digest(driver_secret, line.secret)


async def _decode_correlation(
    x_call: str | None, x_session: str | None,
    x_message: str | None, x_verdict: str | None,
    driver_secret: str | None = None,
) -> dict[str, str | None]:
    """Normalize the four driver correlation headers onto plain ctx values.

    WHY the optional headers ride at all: X-Agent-Call-Id / X-Agent-Session-Id
    let the @track_agent_tool decorator stamp them onto the telemetry_event row
    (analysis joins telemetry ↔ the driver log's tool-call ids) and key the approval
    identity; X-Agent-Message-Id names the assistant row a DETACHED generation
    (generate_image) appends its chip to; X-Agent-Verdict (step 7) carries the
    DRIVER's claim that dsh's approval service already asked the user for THIS
    call and the answer was allowed-once — the replacement for the
    verdict_approved flag the deleted backend hold used to set itself. That
    claim is only READ when the request is driver-attested (see
    driver_attested); an unattested marker is DROPPED, so the call falls back
    to the confirm cell and refuses with 409 as if it had never been marked.
    The marker only ever converts a confirm cell to auto (see
    _resolve_apply_or_force); every RBAC / pin gate and the is_system refusal
    still apply. Absent headers stay None — purely additive, no behavior
    change for tools that never read them.
    """
    return {
        "call_id": x_call if isinstance(x_call, str) else None,
        "session_id": x_session if isinstance(x_session, str) else None,
        "message_id": x_message if isinstance(x_message, str) else None,
        "verdict": (
            x_verdict
            if isinstance(x_verdict, str)
            and await driver_attested(driver_secret)
            else None
        ),
    }


async def get_agent_context(
    authorization: str = Header(...),
    x_agent_call_id: str | None = Header(default=None, alias="X-Agent-Call-Id"),
    x_agent_session_id: str | None = Header(default=None, alias="X-Agent-Session-Id"),
    x_agent_message_id: str | None = Header(default=None, alias="X-Agent-Message-Id"),
    x_agent_verdict: str | None = Header(default=None, alias="X-Agent-Verdict"),
    x_driver_secret: str | None = Header(default=None, alias="X-Driver-Secret"),
) -> dict:
    """Authenticate a project-scoped agent key (Bearer) and read the dsh-driver
    correlation headers onto the ctx. Delegates the
    token → ctx resolution to api_key_auth.authenticate_agent_token (shared with the
    MCP gateway — one auth contract, two surfaces).

    # ARCH: the key carries the OWNING user's identity. We resolve
    # the user here; RBAC is re-checked per target document downstream so rights are
    # strictly bounded — no escalation possible. The surface gate is the key's
    # `capabilities` set (require='agent' in the shared resolver) — one token may be
    # valid on both the widget and agent surfaces.
    #
    # WHY: this Header-based wrapper extracts
    # the Bearer token + the dsh driver correlation headers and delegates; the Tool-
    # API HTTP surface and the MCP surface can never drift.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[7:]
    ctx = await authenticate_agent_token(
        token,
        call_id=x_agent_call_id if isinstance(x_agent_call_id, str) else None,
        session_id=x_agent_session_id if isinstance(x_agent_session_id, str) else None,
    )
    ctx.update(await _decode_correlation(
        x_agent_call_id, x_agent_session_id, x_agent_message_id, x_agent_verdict,
        x_driver_secret,
    ))
    return ctx


async def owned_session_row(ctx: dict) -> dict | None:
    """The caller's OWN live chat session row for ctx["session_id"], or None.

    The ONE ownership predicate for every X-Agent-Session-Id consumer on this
    surface (the pin scope, attachment resolution): the row must exist
    (fetch_one already drops soft-deleted rows), its user_id must equal the
    key's principal, and its project_id must equal the key's project — the same
    user+project pair the image_gen document-parent resolution established
    (routes.tool_api.image_gen), which closes the same-account-other-project
    spelling: a pin must never surface in a project the call is not running in.
    """
    session = await fetch_one("chat_sessions", ctx["session_id"])
    if not session:
        return None
    if session.get("user_id") != ctx["user"]["user_id"]:
        return None
    if session.get("project_id") != ctx.get("project_id"):
        return None
    return session


async def pinned_session_doc_id(ctx: dict) -> str | None:
    """The document a pinned chat session pins, or None when it is not pinned.

    # INVARIANT(security): the pin is read from `chat_sessions.has_region` — SERVER
    # state, never the request body — and honored only on the caller's OWN session.
    # Why: the caller is the party the pin constrains, so deriving "is this session
    # pinned" from what the caller sent lets it opt out by simply omitting `region`.
    # The deleted proposal apply path read `session["has_region"]`; when it went, the
    # direct path was left checking containment only of a region the caller chose to
    # supply, which is not a boundary at all.
    # Why the ownership cell: this read runs BEFORE the hold's session guard, so a
    # foreign pinned session differentiated the answer (pin-specific 409 vs the
    # generic refusal) — an existence/pin oracle on other users' sessions. A foreign
    # or nonexistent session now reads as "no pin" and the hold guard refuses it
    # uniformly downstream.
    """
    session_id = ctx.get("session_id")
    if not session_id:
        return None

    session = await owned_session_row(ctx)
    if not session or not session.get("has_region"):
        return None
    return session.get("document_id") or None
