"""Digest task — the arq entry point of SYSTEM: digest."""
from __future__ import annotations

import logging

from pipeline.core.runs import mark_finished, mark_running

from jobs.tasks._dead_letter import dead_letter

logger = logging.getLogger(__name__)


def digest_job_id(config_id: str) -> str:
    """The arq job id of a digest run — one per binding.

    WHY: every trigger (manual, transcription, schedule) posts under it, so a run
    posted while another is pending collapses onto it instead of a second run
    reading the same window and writing a duplicate report.
    """
    return f"digest:{config_id}"


async def digest_task(ctx, config_id: str, project_id: str, run_id: str | None = None) -> dict:
    """`run_id` (the pipeline_runs log row) is optional — see extract_task."""
    # WHY deferred: pipeline.digest.run → pipeline.extractor.runner → jobs → jobs.tasks
    # → this module; a top-level import is circular when the pipeline is imported first.
    from pipeline.digest.run import create_digest_error_note, run_digest

    if run_id:
        await mark_running(run_id)
    try:
        result = await run_digest(config_id, project_id)
        if run_id:
            if result.get("status") == "written":
                await mark_finished(
                    run_id, "ok", output_doc_id=result.get("document_id"),
                )
            else:
                # "empty" — nothing new in the window: a deliberate no-output run.
                await mark_finished(run_id, "skipped")
        return result
    except Exception as e:
        logger.error("Digest %s failed: %s", config_id, e)
        if run_id:
            await mark_finished(run_id, "error", error=str(e))
        # A failed digest leaves its trace on the THEME document, then arq sees it.
        await dead_letter(create_digest_error_note(config_id, project_id, e), e)
