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


# ─── the first row ────────────────────────────────────────────────────────────


async def test_extractor_is_the_first_registry_row():
    """The extractor is registered, unchanged in behavior: the row's entry IS the
    arq-registered extract_task (not a copy), on the default queue, with both IO
    descriptors stated."""
    from pipeline.core.registry import PIPELINES

    from jobs.tasks import extract_task

    assert len(PIPELINES) >= 1
    row = next(r for r in PIPELINES if r.name == "extractor")
    assert row.entry is extract_task
    assert row.queue == "default"
    assert row.params_source.strip()
    assert row.result_target.strip()
    # The project-scoped params declare what create/claim validation binds —
    # the schedule path's stand-in for the interactive triggers' access checks.
    assert set(row.project_scoped_params) == {
        "project_id", "reference_id", "source_doc_id",
        "config_doc_id", "target_doc_id",
    }


# ─── totality, both directions ────────────────────────────────────────────────


async def test_every_registry_row_is_registered_on_its_declared_queue():
    """Direction A: a row whose task is not registered on its declared queue would
    enqueue and never run (arq's silent drop) — the registry must refuse it."""
    from pipeline.core.registry import validate_registry

    default, transcription = _worker_fn_names()
    validate_registry(default_fn_names=default, transcription_fn_names=transcription)


async def test_pipeline_package_exports_match_registry_rows():
    """Direction B (both ways), over the DERIVED package set: every pipeline
    package's PIPELINE_TASKS export has a registry row, and every registry row's
    task is exported by some pipeline package — no phantom rows, no forgotten
    pipelines."""
    import importlib
    import pkgutil

    import pipeline

    exported: set[str] = set()
    for mod_info in pkgutil.iter_modules(pipeline.__path__):
        if not mod_info.ispkg:
            continue
        sub = importlib.import_module(f"pipeline.{mod_info.name}")
        exported.update(getattr(sub, "PIPELINE_TASKS", ()))

    from pipeline.core.registry import PIPELINES

    registered = {r.entry.__name__ for r in PIPELINES}
    assert exported == registered, (
        f"pipeline packages export {sorted(exported)} but the registry carries "
        f"{sorted(registered)} — add the missing row or drop the stale export"
    )


# ─── loud failure on a malformed row ─────────────────────────────────────────


def _make_rows(**overrides):
    """One synthetic well-formed row, with fields overridable per test."""
    from pipeline.core.registry import PipelineSpec

    async def _fake_task(ctx):
        return None

    _fake_task.__name__ = "fake_pipeline_task"
    fields = {
        "name": "fake",
        "entry": _fake_task,
        "queue": "default",
        "params_source": "test source",
        "result_target": "test target",
    }
    fields.update(overrides)
    return [PipelineSpec(**fields)]


async def test_unregistered_task_fails_loud():
    """A row naming a task the declared worker never registered is refused at
    validation — the row is named in the failure so the fix is obvious."""
    from pipeline.core.registry import validate_registry

    default, transcription = _worker_fn_names()
    with pytest.raises(AssertionError) as exc:
        validate_registry(
            rows=_make_rows(), default_fn_names=default,
            transcription_fn_names=transcription,
        )
    assert "fake" in str(exc.value)


async def test_unknown_queue_fails_loud():
    from pipeline.core.registry import validate_registry

    default, transcription = _worker_fn_names()
    with pytest.raises(AssertionError) as exc:
        validate_registry(
            rows=_make_rows(queue="nonexistent"),
            default_fn_names=default, transcription_fn_names=transcription,
        )
    assert "queue" in str(exc.value)


async def test_duplicate_names_fail_loud():
    from pipeline.core.registry import validate_registry

    rows = _make_rows() + _make_rows()
    default, transcription = _worker_fn_names()
    # Admit the synthetic task on the default queue so ONLY the duplicate name
    # can fail this validation.
    default = default | {"fake_pipeline_task"}
    with pytest.raises(AssertionError) as exc:
        validate_registry(
            rows=rows, default_fn_names=default,
            transcription_fn_names=transcription,
        )
    assert "duplicate" in str(exc.value)


async def test_blank_descriptor_fails_loud():
    from pipeline.core.registry import validate_registry

    default, transcription = _worker_fn_names()
    with pytest.raises(AssertionError):
        validate_registry(
            rows=_make_rows(params_source="   "),
            default_fn_names=default, transcription_fn_names=transcription,
        )


async def test_worker_boot_gate_checks_the_registry():
    """The boot gate (validate_queue_config) validates the registry rows too, so a
    malformed row crashes the worker at startup instead of silently shipping."""
    from pipeline.core import registry

    from jobs.worker import validate_queue_config

    called = {}

    def _spy(rows=None, default_fn_names=None, transcription_fn_names=None):
        called["rows"] = rows
        return None

    original = registry.validate_registry
    registry.validate_registry = _spy
    try:
        validate_queue_config()
    finally:
        registry.validate_registry = original
    assert called["rows"] is registry.PIPELINES
