"""Migration: user_tier_backfill — stamp every tier-less user row 'ai'.

# ARCH: users.tier arrives with DEFAULT 'ai', but the DEFAULT applies only on
#   CREATE — rows predating the column keep NONE until this sweep. Every
#   existing user becomes 'ai' (the plan's decision: the backfill preserves
#   today's behaviour — nobody loses a feature they had).

Idempotent: re-runs find no NONE row and do nothing.
"""

from __future__ import annotations

from migrations._shared import logger


async def _migrate_user_tier_backfill(db) -> None:
    """UPDATE users SET tier = 'ai' WHERE tier IS NONE."""
    rows = await db.query(
        "UPDATE users SET tier = 'ai' WHERE tier IS NONE RETURN AFTER",
    )
    n = len(rows or [])
    if n:
        logger.info("user_tier_backfill: stamped tier='ai' on %d user row(s)", n)
