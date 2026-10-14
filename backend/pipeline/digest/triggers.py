"""Digest triggers — the manual run and the transcription hook post one digest_task per binding."""
from __future__ import annotations

import logging

from db import extract_id, fetch_one, get_db
from jobs import pool as jobs_pool
from jobs.tasks.digest import digest_job_id
from models import is_ref_row
from pipeline.digest.run import create_digest_error_note

logger = logging.getLogger(__name__)


async def enqueue_digest(config_id: str, project_id: str) -> None:
    await jobs_pool.enqueue(
        "digest_task", config_id=config_id, project_id=project_id, job_id=digest_job_id(config_id),
    )


async def on_transcription_complete(reference_id: str, project_id: str, **_kwargs) -> None:
    """Event hook: a reference attached to a theme document finished transcribing →
    every digest bound to that document runs. Never raises.

    WHY a hook of its own beside the extractor's (pipeline/extractor/runner.py): the
    two read disjoint agent_configs rows (kind) and post different tasks; the bus fans
    the one event out to both.
    """
    try:
        ref = await fetch_one("documents", reference_id)
        host_id = extract_id((ref or {}).get("parent_id")) if ref and is_ref_row(ref) else None
        if not host_id:
            return
        db = await get_db()
        rows = await db.query(
            "SELECT id, project_id FROM agent_configs WHERE document_id = $did AND kind = 'digest' "
            "AND trigger_event = 'transcription_complete' AND deleted_at IS NONE",
            {"did": host_id},
        )
    except Exception as e:
        logger.error("Digest hook lookup failed for ref %s: %s", reference_id, e)
        return
    for row in rows or []:
        config_id = extract_id(row["id"])
        row_project_id = extract_id(row.get("project_id")) or extract_id(project_id)
        try:
            await enqueue_digest(config_id, row_project_id)
            logger.info("Auto-triggered digest %s for ref %s", config_id, reference_id)
        except Exception as e:
            logger.error("Digest hook enqueue failed for %s: %s", config_id, e)
            # The user bound this digest to run on new references; a run that never
            # posts leaves its trace on the theme document, not only in the log.
            await create_digest_error_note(config_id, row_project_id, e)
