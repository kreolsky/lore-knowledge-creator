"""Migration: seed_memory_persona — give every project with no persona the unselected "Memory Brain".

New projects get it when their Personas folder is created (ensure_agent_system_docs);
this entry covers projects whose folder already exists and holds no live child.
Nothing is selected — Default stays every chat's persona until a person picks it.

Idempotent: seed_memory_persona skips a folder with any live child; recorded once,
so a persona the user deletes later is never re-seeded.
"""

from __future__ import annotations

from agent_config_seed import seed_memory_persona

from migrations._shared import logger


async def _migrate_seed_memory_persona(db) -> None:
    """Seed the persona under every live Personas folder that has no live child."""
    folders = await db.query(
        "SELECT meta::id(id) AS id, project_id FROM documents "
        "WHERE system_role = 'system_prompt' AND is_system = true AND deleted_at IS NONE",
    ) or []
    seeded = 0
    for folder in folders:
        seeded += await seed_memory_persona(folder["project_id"], folder["id"])
    logger.info("seed_memory_persona: %d of %d project(s) seeded", seeded, len(folders))
