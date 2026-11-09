"""Shared API-key auth — Bearer token → unified key context (widget + agent).

# SYSTEM: api-key-auth — the single token→ctx resolver shared by the widget
# surface (/api/widget/*) and the agent surfaces (Tool-API HTTP /api/tool/* +
# the MCP gateway /mcp).

# ARCH: homed at the backend top level (next
# to rate_limit.py / deps.py), NOT in routes/api_keys.py. Why: the resolver is
# consumed by three route modules (widget, tool_api, api_keys) AND the MCP
# package; homing it in any one of them recreates the layering leak in a new
# direction. This is a leaf module (module-level imports: stdlib + db/deps/
# rate_limit only) so importing it eagerly is cheap and cycle-free.

# ARCH: ONE key row carries a `capabilities`
# set (non-empty subset of {widget, agent}) — a single token may be valid on
# both surfaces. The surface gate is pure set membership (`require` must be in
# the key's capabilities); the liveness check is keyed on the surface BEING
# USED (`require`), never on the other capability, so a combined key stays
# usable on one surface when the other surface's precondition fails.
# document_id is the universal scope root: widget = the bound doc, agent =
# subtree root ('' = whole project, internal agent-minted keys only).

# INVARIANT(security): no privilege escalation / stale-credential IDOR — a key carries the
# OWNING user's identity; there is no synthetic user.
# Why: identity from the key's owner means a revoked member loses access on the
# next call rather than at key expiry. The key grants identity,
# and on the AGENT surface live RBAC re-asserts it on EVERY call (a member
# removed from the project keeps a live api_keys row until expiry — the per-call
# project-membership check revokes access immediately). No result is cached
# across calls. The widget surface instead asserts the bound document still
# exists (doc-liveness), preserving the widget resolver's historical contract.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException
from rate_limit import check_api_key_rate_limit

from access import get_project_access
from db import extract_id, fetch_one, get_db, parse_iso

logger = logging.getLogger(__name__)


def mint_token() -> tuple[str, str]:
    """Mint one api_keys token: returns (plaintext, token_hash).

    # ARCH: the single mint
    # primitive lives HERE, next to resolve_api_key — mint and verify are the two
    # halves of one credential contract, so they share a module. Both issuance
    # sites (agent.keys.create_agent_key_row, api_keys.create_api_key) call this;
    # their EXPIRY logic intentionally differs (internal TTL default vs
    # user-chosen) and is NOT unified here — divergence is documented, not merged.

    The plaintext is returned ONCE (it is unrecoverable from the hash); callers
    must persist only the hash and hand the plaintext back to the user exactly
    once at issuance.
    """
    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token, token_hash


async def resolve_key_principal(user_id: str, *, internal: bool) -> dict:
    """Build the user dict RBAC helpers expect for a key's owner. The internal
    agent key carries the owner's LIVE instance role; every other key is 'user'."""
    row = await fetch_one("users", user_id)
    name = (row or {}).get("name") or "agent"
    # INVARIANT(security): only the INTERNAL agent key carries its owner's live
    # instance role; external MCP/widget keys and upload tokens resolve as 'user'.
    # Why: operator rule — "an admin has full access whatever the member level;
    # what I see is what I have": the agent in an admin's chat gets the admin's UI
    # rights. External keys are narrowed because they outlive a session and get
    # copied: a leaked admin key must not bring admin write, the sandbox or a tier
    # bypass with it. Reading the role live keeps a demotion effective on the
    # internal key's next call.
    role = (row or {}).get("role") if internal else None
    return {"user_id": user_id, "name": name, "role": role or "user"}


async def resolve_api_key(
    token: str, *, require: str,
    call_id: str | None = None, session_id: str | None = None,
) -> dict:
    """Resolve a Bearer API key (plaintext) to a unified key context.

    Shared by the widget surface (get_api_key_context) and the agent surfaces
    (authenticate_agent_token). `require` names the surface being used
    ('widget' | 'agent'); the key is accepted iff `require` is in its
    `capabilities` set. Performs the full check, in order: token hash lookup,
    rate limit, expiry, per-surface liveness (widget: bound doc exists; agent:
    live project-membership re-check), THEN the last-used stamp — so a rejected
    probe (e.g. a revoked member) does NOT register as use.

    Raises HTTPException(401/403/410/429) on any failure. Returns a ctx dict:
      {user_id, project_id, scope_root, key_id, key_label, capabilities,
       auto_apply,
       user, call_id, session_id}.

    `scope_root` = api_keys.document_id ('' = whole project, internal keys).
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    # Key the bucket on the FULL token hash, not a 32-bit [:8] prefix
    # — a truncated prefix collides across distinct keys (two keys sharing an
    # 8-hex-char prefix would share one rate-limit bucket, letting one starve the
    # other). The full hash is per-key.
    if not await check_api_key_rate_limit(f"apikey:{token_hash}"):
        raise HTTPException(status_code=429, detail="Too many requests")

    db = await get_db()
    rows = await db.query(
        "SELECT id, user_id, project_id, document_id, label, capabilities, "
        "expires_at, auto_apply, internal FROM api_keys "
        "WHERE token_hash = $hash AND deleted_at IS NONE LIMIT 1",
        {"hash": token_hash},
    )
    if not rows:
        raise HTTPException(status_code=401, detail="Invalid API key")

    key = rows[0]
    key_id = extract_id(key["id"])

    # Surface gate: pure capability-set membership.
    capabilities = key.get("capabilities") or []
    if require not in capabilities:
        raise HTTPException(
            status_code=403,
            detail=f"This key does not carry the '{require}' capability",
        )

    expires_at = key.get("expires_at")
    if expires_at:
        if isinstance(expires_at, str):
            expires_at = parse_iso(expires_at)
        if expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="API key expired")

    # scope_root = document_id (empty = whole project for agent keys).
    scope_root = key.get("document_id") or ""

    user = await resolve_key_principal(key["user_id"], internal=key.get("internal") is True)

    # Per-surface liveness — keyed on `require` ONLY (never the other
    # capability), so a combined key survives on one surface when the other
    # surface's precondition fails.
    if require == "widget":
        # The widget surface is bound to one document — assert it still exists.
        doc = await fetch_one("documents", scope_root)
        if not doc or doc.get("deleted_at"):
            raise HTTPException(status_code=410, detail="Document has been deleted")
        # …and that its project is alive: project delete marks only the project
        # row (the document keeps deleted_at = NONE), so the document check
        # alone would keep a dead project's widget alive. Same 410 family.
        widget_pid = doc.get("project_id")
        if not widget_pid or not await fetch_one("projects", widget_pid):
            raise HTTPException(status_code=410, detail="Project has been deleted")
    else:
        # INVARIANT (stale-credential IDOR): re-verify CURRENT membership here on
        # the agent surface — the key grants identity, live RBAC re-asserts it on
        # every call. Mutating endpoints additionally re-check per-doc access
        # downstream.
        if await get_project_access(key["project_id"], user) is None:
            raise HTTPException(status_code=403, detail="Agent key no longer has project access")

    # Stamp last_used_at only AFTER the full liveness check passes. Why: a
    # revoked member's automated probe would otherwise keep the key looking
    # 'active' (last_used_at = recent) in the key list — a stale signal. The
    # stamp belongs to a SUCCESSFUL resolution, not a rejected one.
    # (plan "mcp-gateway-debt-paydown", Decision 7.)
    await db.query(
        "UPDATE type::record('api_keys', $id) SET last_used_at = time::now()",
        {"id": key_id},
    )

    # auto_apply is read RAW (no bool coercion) so a migration that did not take
    # surfaces (None) rather than being masked — the gateway treats only an
    # explicit True as granted. Inherited onto ctx by BOTH surfaces; ONLY the
    # gateway mutating dispatch acts on it.
    #
    # WHY (S1 attribution axes): `created_by` = the key's user_id (the
    # RIGHTS axis); `created_by_name` on agent writes = the making key's LABEL.
    # Why: an agent write is made on the owning user's authority and that stays
    # literally true — only the byline (the DISPLAY axis) changes; with N agents
    # depositing into one base, "who put this here" must be answerable at a glance
    # without changing who is accountable for the write.
    # key_label is None for internal keys (their label is dsh-driver plumbing, not
    # an author identity) and for blank/whitespace labels, so those writes keep
    # the owner's name byte-identically. Write sites resolve the byline via
    # agent_author_name(ctx) — never read the raw label off the key row again.
    # DEBT (attribution spoofing): the label is a free-form user-controlled string
    # with no disambiguation from human display names, so a key labeled "Alice"
    # renders every deposit as authored by Alice (the rights axis records the
    # truth; the display axis can be made to lie). Residual owner decision: render
    # agent bylines distinctly (badge/suffix) or reject label↔user-name collisions
    # at key mint/rename. Why deferred: both are product-surface changes outside
    # the S1 backend diff; the risk is display-only until one lands.
    label = key.get("label")
    if key.get("internal") is True or not isinstance(label, str):
        key_label = None
    else:
        key_label = label.strip() or None
    return {
        "user_id": key["user_id"],
        "project_id": key["project_id"],
        "scope_root": scope_root,
        "key_id": key_id,
        "key_label": key_label,
        "capabilities": capabilities,
        "auto_apply": key.get("auto_apply"),
        # `internal` marks the key the dsh driver mints for itself. Read RAW (no bool
        # coercion) for the same reason as auto_apply above: a row predating the
        # field surfaces as None rather than being masked into a confident False.
        # Every gate on it (sandbox console, chat-recall pack, chat attachments,
        # MCP inbox flag) treats ONLY an explicit True as internal.
        "internal": key.get("internal"),
        "user": user,
        "call_id": call_id,
        "session_id": session_id,
    }


async def authenticate_agent_token(
    token: str, *, call_id: str | None = None, session_id: str | None = None,
) -> dict:
    """Resolve an agent-capable key (plaintext) to an agent context.

    Shared by the Tool-API Header-based path (get_agent_context) and the MCP
    per-call path. Thin wrapper: `resolve_api_key(require='agent')` — a key
    without the agent capability is rejected with 403 there.
    """
    return await resolve_api_key(
        token, require="agent", call_id=call_id, session_id=session_id,
    )


def agent_author_name(ctx: dict) -> str | None:
    """The display author name for a write made through an agent key.

    Resolution chain: the making key's label → ctx["user_name"] (the arq worker's
    rebuilt minimal ctx carries the name there) → the user dict's name. ONE
    resolution point for every agent-surface write site.
    # INVARIANT(security): never a second inline fallback chain at any write site.
    # Why: one resolution point keeps every site's byline semantics identical
    # (internal keys, blank labels, legacy rows all fall back the same way); a
    # second inline chain at any site would drift. Callers must never conflate
    # this with the rights axis (ctx["user_id"] / created_by).
    """
    label = ctx.get("key_label")
    if isinstance(label, str) and label:
        return label
    user_name = ctx.get("user_name")
    if isinstance(user_name, str) and user_name:
        return user_name
    user = ctx.get("user") or {}
    return user.get("name")


__all__ = [
    "mint_token", "resolve_api_key", "authenticate_agent_token", "agent_author_name",
]
