"""One-shot prod migration: blob backfill + inline-content null-out.

Bundles Phase-1 steps 3-4 (see ~/.claude/plans/checkpoint-retention-growth.md):
  1. backfill_cp_blobs_task — give every legacy checkpoint row a content_ref
     (incl. soft-deleted rows), compressing+deduping content into cp_blobs.
  2. migrate_cp_content_nullout.run_migration — verify every row resolves from
     its blob (matching hash), OVERWRITE content to option<string>, then null out
     inline content. Aborts before nulling if any row is unmigrated or unresolved.

Both steps are idempotent and safe to re-run. The null-out is irreversible once it
runs — take a backup first (the deploy already writes one; an extra named backup is
recommended).

Usage (inside the prod backend container):
    docker compose -p lore -f docker-compose.prod.yml exec -T backend \
        python /app/scripts/cp_prod_migrate.py

Pass --backfill-only to run step 1 without the null-out (for example, to honor a
verification window before the irreversible step).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

sys.path.insert(0, "/app")

from db import get_db
from jobs.tasks import backfill_cp_blobs_task
from migrations.migrate_cp_content_nullout import run_migration

logger = logging.getLogger("cp_prod_migrate")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def _unmigrated_count(db) -> int:
    rows = await db.query(
        "SELECT count() AS c FROM checkpoints "
        "WHERE content IS NOT NONE AND content_ref IS NONE GROUP ALL"
    )
    return rows[0]["c"] if rows else 0


async def main(backfill_only: bool) -> None:
    db = await get_db()

    before = await _unmigrated_count(db)
    logger.info("Step 1/2 — blob backfill (unmigrated rows: %d)", before)
    await backfill_cp_blobs_task({})
    after = await _unmigrated_count(db)
    logger.info("Backfill done (unmigrated rows now: %d)", after)
    if after:
        raise RuntimeError(f"{after} row(s) still unmigrated after backfill — aborting")

    if backfill_only:
        logger.info("--backfill-only: skipping null-out (run again without the flag to finish)")
        return

    logger.info("Step 2/2 — inline-content null-out")
    await run_migration()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prod cp_blobs backfill + content null-out")
    parser.add_argument("--backfill-only", action="store_true",
                        help="run backfill only, skip the irreversible null-out")
    args = parser.parse_args()
    asyncio.run(main(args.backfill_only))
