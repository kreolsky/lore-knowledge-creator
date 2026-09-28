"""Extractor task — the arq entry point of SYSTEM: extractor."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def extract_task(
    ctx,
    reference_id: str,
    source_doc_id: str,
    config_doc_id: str,
    target_doc_id: str,
    project_id: str,
    title_template: str | None = None,
    model: str | None = None,
    user_id: str | None = None,
) -> None:
    from pipeline.extractor.runner import _create_error_note, run_extractor

    from jobs.tasks._dead_letter import dead_letter

    try:
        await run_extractor(
            reference_id, source_doc_id, config_doc_id, target_doc_id, project_id,
            title_template=title_template, model=model, user_id=user_id,
        )
    except Exception as e:
        logger.error("Extractor pipeline failed for ref %s: %s", reference_id, e)
        # Terminal dead-letter: WHEN is dead_letter()'s contract. The recorder is
        # the error note (a failed extraction must leave a visible trace on the
        # SOURCE document), then arq sees the failure.
        await dead_letter(
            _create_error_note(
                project_id, source_doc_id, reference_id, e,
                config_doc_id=config_doc_id,
            ),
            e,
        )
