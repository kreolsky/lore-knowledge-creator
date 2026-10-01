"""Migration: reference_sort_keys_backfill — key every reference row.

# ARCH: references are in the fractional-order world: every live reference
#   row gets a sort_key in its own (project_id, parent_id, is_reference) group. This one-time backfill covers
#   rows created before the change; fresh rows are keyed at creation
#   (documents.service.create_reference_row / create_document).

Order per group is updated_at DESC (newest first), id tie-broken — freezing the
panel order users saw before the deploy, so the backfill moves nothing.
Archived refs get keys too (the ref key space includes them; they sort among
themselves below live via the archived sink).

Idempotent: NONE rows only, so a second run updates nothing.
"""

from __future__ import annotations

from sort_keys import assign_sort_keys_to_none_rows

from migrations._shared import logger


async def _migrate_reference_sort_keys_backfill(db) -> None:
    """Key every NONE-key reference row; log the count."""
    # Key write only — no updated_at bump (position is not an edit; the panel
    # treats a ref's updated_at as its content version).
    updated = await assign_sort_keys_to_none_rows(db, is_reference=True)
    logger.info("reference_sort_keys_backfill: keyed %d reference row(s)", updated)
