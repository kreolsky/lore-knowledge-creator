"""Migration: turn_timeout_override_fold — a TURN_TIMEOUT_S override moves to the grace key.

# ARCH: TURN_TIMEOUT_S (the legacy spelling of the turn's no-progress budget)
#   is no longer a setting; TURN_PROGRESS_GRACE_S owns the budget and its
#   default. An admin override stored under the old key would otherwise be
#   ignored silently, so it is carried onto the grace key and the old row is
#   deleted.

# INVARIANT(persisted): an existing TURN_PROGRESS_GRACE_S override wins — the
# old row is then only deleted, never copied over it.
# Why: the grace's own row already outranked the base's when both existed, so
# the budget the turn actually ran with must not change under the operator.

Idempotent: a re-run finds no TURN_TIMEOUT_S row and does nothing.
"""

from __future__ import annotations

from migrations._shared import logger

_OLD = "TURN_TIMEOUT_S"
_NEW = "TURN_PROGRESS_GRACE_S"


async def _migrate_turn_timeout_override_fold(db) -> None:
    """Carry a TURN_TIMEOUT_S override onto TURN_PROGRESS_GRACE_S (unless the
    grace has its own), then delete the TURN_TIMEOUT_S row."""
    rows = await db.query(
        "SELECT key, value, updated_by FROM instance_settings WHERE key IN [$old, $new]",
        {"old": _OLD, "new": _NEW},
    )
    by_key = {row["key"]: row for row in rows or []}
    old = by_key.get(_OLD)
    if old is None:
        return
    if _NEW not in by_key and old.get("value") is not None:
        await db.query(
            "UPSERT type::record('instance_settings', $key) SET "
            "key = $key, value = $value, updated_by = $by, updated_at = time::now()",
            {"key": _NEW, "value": old["value"], "by": old.get("updated_by") or "migration"},
        )
        logger.info(
            "turn_timeout_override_fold: carried the %s override %s onto %s",
            _OLD, old["value"], _NEW,
        )
    await db.query("DELETE type::record('instance_settings', $key)", {"key": _OLD})
