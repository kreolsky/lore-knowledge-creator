"""S2 (plan 1786740208300) — pipeline registry totality + loud-failure gates.

# INVARIANT: a registry row describes the ARQ TASK (the enqueue address), not the
# PocketFlow flow inside it. Why: the task is what a schedule (S3) addresses and
# what the worker registers; the flow is an internal of the extractor task and a
# future pipeline may have none.

The registry adds NO dispatch indirection — callers keep enqueueing `extract_task`
directly; the row is addressable metadata + totality guards so a pipeline task can
never drift out of worker registration (the arq silent-drop failure the boot gates
exist for).
"""

import pytest

pytestmark = pytest.mark.asyncio


def _worker_fn_names():
    """The real registered-task names per queue, DERIVED from the worker classes
    (not a literal copy — the assertion must bind registry ↔ registration)."""
    from jobs.worker import TranscriptionWorkerSettings, WorkerSettings

    default = {getattr(f, "name", None) for f in WorkerSettings.functions}
    transcription = {
        getattr(f, "name", None) for f in TranscriptionWorkerSettings.functions
    }
    return default, transcription


# ─── totality, both directions ────────────────────────────────────────────────


async def test_every_registry_row_is_registered_on_its_declared_queue():
    """Direction A: a row whose task is not registered on its declared queue would
    enqueue and never run (arq's silent drop) — the registry must refuse it."""
    from pipeline.core.registry import validate_registry

    default, transcription = _worker_fn_names()
    validate_registry(default_fn_names=default, transcription_fn_names=transcription)
