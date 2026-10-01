"""Migration: web_search_off_drop — delete a persisted WEB_SEARCH_PROVIDER `off`.

# ARCH: `off` stopped being a WEB_SEARCH_PROVIDER choice (web search has no
#   off state — the tool is always offered, and to stop searching an admin
#   clears the key). Override rows are read WITHOUT validation
#   (settings.load_overrides decodes whatever is stored), so a stored `off`
#   would reach the turn-payload builder and break every turn with a KeyError.
#   Deleting the row lets the default (deepseek) apply.

# INVARIANT(persisted): delete ONLY the `off` row — any other stored value
# (`brave`, …) is a live admin choice and stays untouched.
# Why: the migration exists to remove a value the registry can no longer
# resolve, not to reset the operator's choice.

Idempotent: a re-run finds no row (or a non-`off` one) and does nothing.
"""

from __future__ import annotations

import json

from migrations._shared import logger

_KEY = "WEB_SEARCH_PROVIDER"


async def _migrate_web_search_off_drop(db) -> None:
    """Delete the `off` override row for WEB_SEARCH_PROVIDER, if that is what
    is stored."""
    rows = await db.query(
        "SELECT key, value FROM instance_settings WHERE key = $key",
        {"key": _KEY},
    )
    raw = rows[0]["value"] if rows else None
    if raw is None:
        return
    try:
        stored = json.loads(raw)
    except ValueError:
        logger.warning(
            "web_search_off_drop: unparsable %s override left untouched: %r",
            _KEY, raw,
        )
        return
    if stored != "off":
        return
    await db.query(
        "DELETE type::record('instance_settings', $key)", {"key": _KEY},
    )
    logger.info(
        "web_search_off_drop: deleted the persisted WEB_SEARCH_PROVIDER='off' "
        "override (no longer a choice; the default deepseek applies)",
    )
