"""Agent-key minting + plaintext cache.

# SYSTEM: agent-keys — the internal project-scoped agent credential (api_keys
# row, internal=true, document_id='' = whole project) and its Redis plaintext
# cache. `label` is pure display — never a discriminator.

# ARCH: this is the SINGLE home for agent-key internals — routes.tool_api
# imports the minting primitive from here (never the reverse: the tool-api
# boundary module must not be imported for its privates). The `/keys` REST
# route stays in routes.tool_api (it owns the tool-api router).

# INVARIANT: an agent key carries the OWNING user's identity (resolved by the
# tool-api auth layer); there is no synthetic user with independent grants.  Why: the agent acts as the owning user (no synthetic identity), so RBAC stays real — the key resolves to a user with their actual grants, not a bypass. The
# per-session standing key's plaintext is cached in Redis with a TTL so a
# warm session does not re-mint / write to the DB every turn; the driver's
# tool ctx takes the per-session key.
"""
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import redis_pool

from db import create_record, extract_id, get_db

logger = logging.getLogger(__name__)

# Display-only default label for internally-minted keys. NEVER a discriminator —
# key semantics live in `capabilities` + `internal`.
AGENT_KEY_LABEL = "agent"

# Default TTL for internally-minted agent keys (the dsh driver path). Honored by the
# shared create_agent_key_row so all three minting call sites agree on expiry.
AGENT_KEY_DEFAULT_TTL_DAYS = 90

_AGENT_KEY_CACHE_TTL = 3600


# ARCH: create_agent_key_row is PUBLIC (no underscore) — routes.tool_api
# reaches it from outside the agent package (the module-boundary test requires
# the public surface). It is the single source of truth for agent-key minting
# (review: expiry drift): a new internal (dsh-driver) row, or a token rotation
# of an existing one (reuse_key_id). `expires_in` is a timedelta (default 90
# days; None = no expiry) — the day-granularity REST model `expires_in_days`
# stays untouched. `scope_root` narrows to a subtree ('' = whole project);
# `chat_session_id` discriminates the per-session standing key (
# None = every other key kind). The row is capabilities=['agent'],
# internal=true. Quirk preserved: on the reuse branch `expires_in=None`
# leaves an existing `expires_at` standing, never clears it.
async def create_agent_key_row(
    user_id: str, project_id: str, *,
    expires_in: timedelta | None = timedelta(days=AGENT_KEY_DEFAULT_TTL_DAYS),
    reuse_key_id: str | None = None,
    auto_apply: bool = False,
    scope_root: str = "",
    chat_session_id: str | None = None,
) -> tuple[str, str, str | None]:
    """Mint/rotate one internal agent-key row; returns (token, key_id,
    expires_at_iso). Contract in the ARCH note above."""
    from api_key_auth import mint_token

    token, token_hash = mint_token()
    expires_at_str: str | None = None
    exp = None
    if expires_in is not None:
        exp = datetime.now(timezone.utc) + expires_in
        expires_at_str = exp.isoformat()

    key_id = reuse_key_id or str(uuid4())
    db = await get_db()
    if reuse_key_id:
        await db.query(
            "UPDATE type::record('api_keys', $id) SET token_hash = $h, auto_apply = $aa, "
            "capabilities = ['agent'], internal = true, document_id = $sr, "
            "chat_session_id = $cs"
            + (", expires_at = $exp" if exp else ""),
            {"id": key_id, "h": token_hash, "aa": auto_apply, "sr": scope_root,
             "cs": chat_session_id,
             **({"exp": exp} if exp else {})},
        )
    else:
        await create_record("api_keys", key_id, {
            "user_id": user_id, "project_id": project_id,
            "document_id": scope_root, "token_hash": token_hash, "label": AGENT_KEY_LABEL,
            "capabilities": ["agent"],
            "internal": True,
            "auto_apply": auto_apply,
            "chat_session_id": chat_session_id,
            **({"expires_at": exp} if exp else {}),
        })
    return token, key_id, expires_at_str


async def mint_run_key(
    user_id: str, project_id: str, scope_root: str, ttl: timedelta,
) -> tuple[str, str, str | None]:
    """Mint a run-scoped agent key: a subtree wall + a sub-day TTL in ONE call.

    # ARCH: PUBLIC (no underscore) — the memory package and the future sandbox
    # executor reach it from outside the agent package, so the module-boundary
    # test requires it on the public surface.

    # INVARIANT(security): a run key may NEVER be whole-project — an empty
    # `scope_root` raises ValueError instead of minting.
    # Why: a run key is scoped to the run's subtree (blast-radius hygiene —
    # a memory/sandbox run never needs the whole project, and a whole-project
    # row is what a session-key lookup could adopt + token-rotate from under
    # a live run). Making it unrepresentable beats documenting the hazard.
    # The refusal happens BEFORE any DB write — a refused mint leaves no row.
    """
    if not scope_root:
        raise ValueError(
            "mint_run_key requires a non-empty scope_root: a run key may never "
            "be whole-project (the dsh driver would adopt and token-rotate it)",
        )
    return await create_agent_key_row(
        user_id, project_id, scope_root=scope_root, expires_in=ttl,
    )


# ── The per-chat-session standing key ───────────────────────────────────────
#
# # ARCH: ONE api_keys row per chat session: the row is discriminated by chat_session_id (label is
# # pure display), minted on FIRST use, RENEWED when the Redis plaintext cache
# # goes cold (the row is reused — token rotated, expiry re-extended: the
# # plaintext is unrecoverable from the hash, so a cold cache must mint a fresh
# # token — never a second row), and REVOKED on session delete and on
# # access-level change. The whole-project (user, project) key above is a
# # PARALLEL system: neither lookup can see the other's rows.
#
# # INVARIANT(security): the standing key's blast radius is bounded by a SHORT
# # row TTL (a week, vs the 90-day whole-project default) that only a used
# # session keeps extending. Why: the key travels in every /followup payload
# # and lives in the driver's per-turn tool ctx — a leak is worth exactly as
# # much as the session's remaining validity.

#: Row TTL for a per-session standing key — renewed (rotated + extended) on
#: every cache-cold use; an unused session's key expires on its own.
SESSION_AGENT_KEY_TTL_S = 7 * 24 * 3600


def _session_key_cache_id(chat_session_id: str) -> str:
    return f"agent_key:session:{chat_session_id}"


async def _renew_or_mint_session_key(
    user_id: str, project_id: str, chat_session_id: str,
) -> str:
    """The cache-miss half: reuse the session's live row (token rotated,
    expiry re-extended — the plaintext is unrecoverable from the hash) or
    mint the first one. Audit lines distinguish mint from renew."""
    db = await get_db()
    # WHY created_at in SELECT: SurrealDB requires ORDER BY fields in the
    # projection (strict selection) — same rule as the whole-project lookup.
    rows = await db.query(
        "SELECT id, created_at FROM api_keys "
        "WHERE chat_session_id = $sid AND user_id = $uid AND project_id = $pid "
        "AND internal = true AND deleted_at IS NONE "
        "AND (expires_at IS NONE OR expires_at > time::now()) "
        "ORDER BY created_at DESC LIMIT 1",
        {"sid": chat_session_id, "uid": user_id, "pid": project_id},
    )
    ttl = timedelta(seconds=SESSION_AGENT_KEY_TTL_S)
    if rows:
        token, key_id, _ = await create_agent_key_row(
            user_id, project_id, reuse_key_id=extract_id(rows[0]["id"]),
            expires_in=ttl, chat_session_id=chat_session_id,
        )
        logger.info(
            "agent-key session RENEW sid=%s key=%s ttl=%ss",
            chat_session_id, key_id, SESSION_AGENT_KEY_TTL_S,
        )
    else:
        token, key_id, _ = await create_agent_key_row(
            user_id, project_id, expires_in=ttl, chat_session_id=chat_session_id,
        )
        logger.info(
            "agent-key session MINT sid=%s key=%s ttl=%ss",
            chat_session_id, key_id, SESSION_AGENT_KEY_TTL_S,
        )
    return token


async def get_or_create_session_agent_key(
    user_id: str, project_id: str, chat_session_id: str,
) -> str:
    """Get-or-create the per-chat-session standing agent key, return plaintext.

    # ARCH: PUBLIC — the harness turn path (routes.chat.completions_harness)
    # is the caller; the Redis cache key is per CHAT session, so two sessions
    # of one user never share a plaintext.

    Cache hit → the warm plaintext; miss → renew-or-mint, then re-cache.
    """
    cache_id = _session_key_cache_id(chat_session_id)
    # One plaintext per chat session — the cache is keyed by session, never
    # by (user, project).
    try:
        cached = await (await redis_pool.get_redis()).get(cache_id)
    except Exception:
        logger.warning("session agent-key cache get failed", exc_info=True)
        cached = None
    if cached:
        return cached

    token = await _renew_or_mint_session_key(user_id, project_id, chat_session_id)
    try:
        await (await redis_pool.get_redis()).set(cache_id, token, ex=_AGENT_KEY_CACHE_TTL)
    except Exception:
        logger.warning("session agent-key cache set failed", exc_info=True)
    return token


async def _drop_session_key_caches(chat_session_ids: list[str]) -> None:
    """Best-effort plaintext-cache drop for revoked keys."""
    if not chat_session_ids:
        return
    try:
        r = await redis_pool.get_redis()
        await r.delete(*[_session_key_cache_id(sid) for sid in chat_session_ids])
    except Exception:
        logger.warning("session agent-key cache drop failed", exc_info=True)


async def revoke_session_agent_key(chat_session_id: str) -> int:
    """Soft-delete the chat session's standing key rows + drop the cache.

    Returns the number of rows revoked. Idempotent: an already-revoked (or
    never-minted) session revokes 0 rows."""
    db = await get_db()
    rows = await db.query(
        "UPDATE api_keys SET deleted_at = time::now() "
        "WHERE chat_session_id = $sid AND deleted_at IS NONE",
        {"sid": chat_session_id},
    )
    n = len(rows or [])
    if n:
        await _drop_session_key_caches([chat_session_id])
    logger.info("agent-key session REVOKE sid=%s rows=%d", chat_session_id, n)
    return n


async def revoke_member_session_keys(project_id: str, user_id: str) -> int:
    """Soft-delete EVERY per-session standing key of (user, project).

    The access-level-change revocation: fired from the
    access_changed event every member mutation emits. Whole-project rows are
    untouched — that system keeps its own lifecycle."""
    db = await get_db()
    rows = await db.query(
        "UPDATE api_keys SET deleted_at = time::now() "
        "WHERE project_id = $pid AND user_id = $uid "
        "AND chat_session_id IS NOT NONE AND deleted_at IS NONE "
        "RETURN BEFORE",
        {"pid": project_id, "uid": user_id},
    )
    n = len(rows or [])
    if n:
        await _drop_session_key_caches([
            str(r["chat_session_id"]) for r in rows if r.get("chat_session_id")
        ])
    logger.info(
        "agent-key member-session REVOKE project=%s user=%s rows=%d",
        project_id, user_id, n,
    )
    return n


async def _on_access_changed(*, project_id=None, user_id=None, **_kwargs) -> None:
    """access_changed subscriber: a member's access level changed (or was
    removed) — their standing per-session keys in that project die now, not at
    TTL. RBAC itself resolves per call (the key carries the OWNING user's
    identity), so this is blast-radius hygiene, not a correctness gate."""
    if not project_id or not user_id:
        return
    try:
        await revoke_member_session_keys(str(project_id), str(user_id))
    except Exception:
        logger.warning(
            "access_changed session-key revocation failed project=%s user=%s",
            project_id, user_id, exc_info=True,
        )


# WHY module-level registration: the subscriber must be live from process
# start (the embeddings precedent, main.py) — routes/chat/__init__.py imports
# this module so `on(...)` runs at app load, under BOTH the real lifespan and
# the ASGITransport test client (which skips lifespans).
from event_bus import on as _bus_on  # noqa: E402 — registration at import (see above)

_bus_on("access_changed", _on_access_changed)


__all__ = [
    "AGENT_KEY_LABEL",
    "AGENT_KEY_DEFAULT_TTL_DAYS",
    "SESSION_AGENT_KEY_TTL_S",
    "create_agent_key_row",
    "mint_run_key",
    "get_or_create_session_agent_key",
    "revoke_session_agent_key",
    "revoke_member_session_keys",
]
