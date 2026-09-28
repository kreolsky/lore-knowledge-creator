"""Migration: messages_schemafull_converge — `messages` takes prod's mode everywhere.

# ARCH: `messages` was `TYPE ANY SCHEMALESS` on dev and SCHEMAFULL on prod and
#   gray, because `DEFINE TABLE IF NOT EXISTS … SCHEMAFULL` is a no-op on an
#   existing table and froze each environment at creation time. The divergence
#   is not cosmetic: it hid a production defect. The detached generate_image
#   run's chips stored fine on the SCHEMALESS dev table and were REFUSED on the
#   SCHEMAFULL prod one, so the refiner plate and the image chip were missing
#   from every prod reload and could not be reproduced anywhere else. The same
#   `ALTER TABLE` cure the documents / user_preferences drift got in
#   schema.surql, except it cannot live there — see the WHY below.

Idempotent: the sweep leaves NONE, the ALTER restates a mode the table may
already have. A no-op on prod, gray and every fresh DB.
"""

from __future__ import annotations

from migrations._shared import logger

# INVARIANT(corruption): the sweep of the retired `entry_id` VALUES and the flip to
# SCHEMAFULL are ONE step — a database never boots converged-but-unswept. Why:
# `ALTER TABLE … SCHEMAFULL` succeeds over a row holding an undeclared key and does not
# re-coerce it, so the damage surfaces later — the next per-row UPDATE of that row dies
# with `Found field 'entry_id', but no such field exists`, which is a user editing an old
# chat message and getting an error. Dev carried 1497 such rows. Their ORDER inside this
# function is not what protects anything (a table-wide `SET entry_id = NONE` still
# succeeds after the flip, which is how a wrongly-converged database is recovered);
# sweep-first is kept as the order that also holds if these two ever move apart.
_SWEEP = "UPDATE messages SET entry_id = NONE"

# WHY: the ALTER lives here rather than beside the documents / user_preferences
# ALTERs in schema.surql, because apply_schema runs BEFORE the migration runner
# (main.py lifespan). From the DDL file it would flip the mode on a boot whose sweep
# has not run yet, opening a window in which 1497 rows are SCHEMAFULL and poisoned.
# One migration, sweep first, has no such window.
_ALTER = "ALTER TABLE messages SCHEMAFULL"


async def _migrate_messages_schemafull_converge(db) -> None:
    """Sweep the retired entry_id values, then converge the table (idempotent)."""
    await db.query(_SWEEP)
    await db.query(_ALTER)
    logger.info(
        "messages_schemafull_converge: entry_id values swept and messages converged "
        "to SCHEMAFULL"
    )
