"""Migration: chats_hide_fold — a list-only chat hide becomes the one chat hide.

# INVARIANT(persisted): a document hidden only from the chat list stays hidden.
# Why: `chats_unindexed` now governs the list too, so without the fold the
# retired `chats_unlisted` flag would silently reopen a subtree's chats.

The WHERE stays narrow: a table-wide UPDATE dies on legacy rows (see `archived`
in surreal/schema.surql). Idempotent: a re-run matches no row.
"""

from __future__ import annotations

from migrations._shared import logger


async def _migrate_chats_hide_fold(db) -> None:
    """Set chats_unindexed on rows flagged chats_unlisted only."""
    rows = await db.query(
        "UPDATE documents SET chats_unindexed = true "
        "WHERE chats_unlisted = true AND chats_unindexed != true",
    )
    logger.info("chats_hide_fold: folded %d list-only hides", len(rows or []))
