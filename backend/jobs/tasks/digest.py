"""Digest task — the arq entry point of SYSTEM: digest."""
from __future__ import annotations

import logging

from jobs.tasks._dead_letter import dead_letter

logger = logging.getLogger(__name__)


def digest_job_id(config_id: str) -> str:
    """The arq job id of a digest run — one per binding.

    WHY: every trigger (manual, transcription, schedule) posts under it, so a run
    posted while another is pending collapses onto it instead of a second run
    reading the same window and writing a duplicate report.
    """
    return f"digest:{config_id}"


async def digest_task(ctx, config_id: str, project_id: str) -> dict:
    # WHY deferred: pipeline.digest.run → pipeline.extractor.runner → jobs → jobs.tasks
    # → this module; a top-level import is circular when the pipeline is imported first.
    from pipeline.digest.run import create_digest_error_note, run_digest

    try:
        return await run_digest(config_id, project_id)
    except Exception as e:
        logger.error("Digest %s failed: %s", config_id, e)
        # A failed digest leaves its trace on the THEME document, then arq sees it.
        await dead_letter(create_digest_error_note(config_id, project_id, e), e)
