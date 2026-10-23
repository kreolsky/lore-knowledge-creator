"""User capability tiers — the per-account ladder deciding which costly
features an account may START in ANY project.

# SYSTEM: capability-tiers — base | voice | ai; tier gates at the cost-starting routes
#
# base  = documents, tree, notes (incl. note-chats), search, export.
# voice = base + voice recording, audio upload, any transcription.
# ai    = voice + chat, agent, pipeline launch (incl. extractor previews and
#         widget extraction), image generation.
#
# ARCH: the tier is ONE field on the user (users.tier), not a group grant.
# Model grants (backend/model_access.py) are an additive allow-list with a
# `public` subject — restriction would need deny semantics; a per-account
# ladder is the simplest restricting shape for a small trusted group. Three
# axes, never merged: instance role, account tier, project access. Model
# grants keep narrowing WITHIN `ai`.
#
# ARCH: the gate sits at the route that STARTS the costly thing — creation is
# refused, existing content (audio refs, chats, transcripts) stays readable
# after a downgrade. The worker trusts the enqueue (startup recovery
# re-enqueues as "__recovery__", backend/main.py — a gate inside
# enqueue_transcription would refuse it).
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

TIER_ORDER = ("base", "voice", "ai")


async def user_has_tier(user_id: str, role: str | None, needed: str) -> bool:
    """True iff the account may use features of `needed` tier or below.

    A fresh DB read of users.tier + deleted_at on every call — NOT the 10s
    auth cache (backend/auth.py), so a downgrade bites on the next request
    and auth.py stays untouched. A missing or soft-deleted row is False (the
    same rule as user_subjects, backend/model_access.py).

    `role` is a PARAMETER, never re-read here: a session request passes its
    session role; a key principal (widget, MCP) passes the "user" that
    _resolve_user pins (backend/api_key_auth.py — a key never inherits an
    instance role); the schedule worker passes the creator's DB role. Reading
    the role inside would hand an admin's key the admin bypass. Consequence:
    an admin's key is bounded by that admin's stored tier.
    """
    if role == "admin":
        return True
    from db import get_db

    db = await get_db()
    rows: list[dict[str, Any]] = await db.query(
        "SELECT tier, deleted_at FROM type::record('users', $id)",
        {"id": user_id},
    )
    if not rows or rows[0].get("deleted_at") is not None:
        return False
    # NONE = a row predating the column, before the backfill reaches it — the
    # schema default is 'ai', so None must not lock a user out mid-boot.
    tier = rows[0].get("tier") or "ai"
    # INVARIANT(security): the tier ladder is compared by ORDER, fail-closed on
    # any value outside TIER_ORDER (only the schema ASSERT keeps rows legal).
    # Why: a hand-edited or drifted tier value must refuse, never pass — the
    # comparison is the one line every gated route's security reduces to.
    if tier not in TIER_ORDER or needed not in TIER_ORDER:
        return False
    return TIER_ORDER.index(tier) >= TIER_ORDER.index(needed)


async def require_tier(user: dict, needed: str) -> None:
    """403 `tier_required:<needed>` unless the principal's account carries the
    tier. `user` carries the CALLER'S role axis (session user, key-pinned
    principal, or the schedule creator's DB role) — see user_has_tier."""
    if not await user_has_tier(user.get("user_id", ""), user.get("role"), needed):
        raise HTTPException(status_code=403, detail=f"tier_required:{needed}")


async def require_voice_for_audio_ref(ref: dict, user: dict) -> None:
    """The shared arm-gate for re-processing a reference: audio re-enqueues
    STT, so the principal must hold 'voice' (see SYSTEM: capability-tiers).
    Callers: REST POST /api/references/{id}/retry, MCP reprocess_file,
    Tool-API reprocess_reference."""
    if ref.get("media_type") == "audio":
        await require_tier(user, "voice")
