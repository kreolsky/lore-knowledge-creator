"""The arq terminal-failure contract — ONE home for the dead-letter wording.

# INVARIANT: arq does NOT auto-retry plain exceptions (retry_jobs covers only
# Retry/CancelledError) — an unhandled task exception is TERMINAL on try one.
# Why: every failure path must record its error state BEFORE re-raising — or the
# entity is left stuck at 'processing' with no event, no error note, no failed
# status; job_try never climbs to max_tries, so a `job_try >= max_tries` gate
# would be dead code that skips recording entirely. Before this helper, that
# contract lived in four hand-copied INVARIANT blocks (transcribe / convert_docx
# / extract / embed_document) that drifted in wording. dead_letter() is the
# single place it lives now; each task passes its own recorder (transcription
# error event / _mark_error / error note / embed status) and can no longer drift
# on WHEN recording happens.
"""
from __future__ import annotations

from typing import NoReturn


async def dead_letter(record, exc: BaseException) -> NoReturn:
    """Run the task-specific failure recorder, then re-raise the original.

    `record` is the caller's already-created coroutine (e.g. `_fail_early()` —
    the recorder closure over the task's ids/ctx); None re-raises immediately.
    A BROKEN recorder must not swallow the task's original exception — the
    re-raise is the whole point (arq must see it).
    """
    if record is not None:
        try:
            await record
        except Exception:
            from logging import getLogger

            getLogger(__name__).warning(
                "dead-letter recorder failed (original error follows)",
                exc_info=True,
            )
    raise exc
