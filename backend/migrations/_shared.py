"""Shared infra for the ordered migration runner.

# ARCH: this module is the dependency root of the migrations package. Per-
#   migration modules import ONLY primitives from here (the logger, the
#   MigrationFn type, the applied-set helpers), never from runner.py — so the
#   runner (which imports every per-migration module to build the registry)
#   never creates an import cycle.
#
# INVARIANT: do NOT import runner (or any migrate_* module) here — that would
#   reintroduce the cycle this layer exists to break.  Why: _shared is the bottom of the migration layer that migrate_* modules import; importing runner here pulls the whole runner back into _shared, recreating the import cycle.

Reads and records the applied set in app_meta:migrations.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

# Shared logger. Kept under the historical "migrations.runner" name (not this
# module's __name__) so log records — and the module logger the migration tests
# attach a handler to via `migrations.runner.logger` — stay byte-identical
# post-split. Per-migration modules import THIS object; the runner does too, so a
# handler attached to `runner.logger` captures every migration's log line.
logger = logging.getLogger("migrations.runner")


class MigrationDeferred(Exception):
    """A migration whose precondition is not met yet.
    The runner logs it and does NOT mark the migration applied, so it retries on
    every boot until the precondition is met. The migration's safe/idempotent work
    (run before raising) still takes effect each boot."""


MigrationFn = Callable[[Any], Awaitable[None]]


async def _get_applied(db) -> set[str]:
    """Return the set of already-applied migration names (empty record on a fresh DB)."""
    record = await db.query("SELECT applied FROM app_meta:migrations")
    if record and record[0].get("applied") is not None:
        return set(record[0]["applied"])

    # UPSERT (not CREATE) — two replicas booting concurrently both see no record via
    # SELECT; CREATE would throw AlreadyExistsError on the second, crashing boot.
    await db.query("UPSERT app_meta:migrations SET applied = []")
    return set()


async def _record_applied(db, name: str) -> None:
    """Mark a single migration as applied by appending to the set."""
    await db.query(
        "UPDATE app_meta:migrations SET applied = array::union(applied, [$name])",
        {"name": name},
    )
