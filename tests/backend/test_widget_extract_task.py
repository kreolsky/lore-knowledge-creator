"""widget_extract_task — STT + dry extractor on one arq job, no document.

Plan widget-extract-batch-api: the task transcribes the widget recording, writes
the transcript through set_content (backplane-visible), then runs the DRY
extractor and stores variables in file_meta.extract. It NEVER emits
transcription_complete (that is the wet hook's trigger) and never creates a
document.
"""

import pytest
from emit_recorder import EmitRecorder

PID = "wex-task-proj"
SRC_DOC = "wex-task-src"
REF_ID = "wex-task-ref"
FILE_REL = f"{PID}/{REF_ID}/rec.webm"


async def _make_ref(test_db):
    """A widget-extract reference row + its stored audio file + initialised extract state."""
    import os
    from pathlib import Path

    from config import STORAGE_PATH
    from db import create_record, get_db

    # Task tests have no `client` fixture, so no per-test wipe: delete own rows
    # first (same ids every test — one FIXED identity, not per-test uuid noise).
    await test_db.query(
        "DELETE type::record('documents', $a) ; DELETE type::record('documents', $b)",
        {"a": SRC_DOC, "b": REF_ID},
    )
    await create_record("documents", SRC_DOC, {
        "project_id": PID, "parent_id": None, "title": "Sandbox",
        "content": "", "path": f"{SRC_DOC}.md", "is_index": False,
    })
    await create_record("documents", REF_ID, {
        "project_id": PID, "parent_id": SRC_DOC, "title": "rec.webm",
        "content": "", "path": f"_ref/{REF_ID}.md",
        "is_reference": True, "media_type": "audio",
        "file_path": FILE_REL, "processing_status": "queued",
    })
    abs_dir = Path(STORAGE_PATH) / PID / REF_ID
    os.makedirs(abs_dir, exist_ok=True)
    (abs_dir / "rec.webm").write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 96)
    # The route's ONE whole-object init write, replayed here (the task only
    # ever writes nested paths after it).
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET file_meta.extract = {"
        "status: 'queued', session: {id: 'sess-task'}, started_at: time::now()}",
        {"id": REF_ID},
    )


class _QuerySpy:
    """Records every SQL string executed through the shared timed proxy."""

    def __init__(self):
        self.sqls: list[str] = []

    def install(self, db_proxy):
        import functools

        original = db_proxy.query

        @functools.wraps(original)
        async def spy(sql, params=None, *, site="unspecified"):
            self.sqls.append(sql)
            return await original(sql, params, site=site)

        db_proxy.query = spy
        return lambda: setattr(db_proxy, "query", original)


async def _run_task(task_kwargs=None, *, transcribe_return="transcript text",
                    transcribe_error=None, dry_return=None, dry_error=None):
    """Run widget_extract_task with the four patchable seams, return what happened."""
    from unittest.mock import AsyncMock, patch

    from jobs.tasks.widget_extract import widget_extract_task

    dry_return = dry_return or {
        "extracted_data": {"Doctor": "House"}, "rendered_markdown": "", "variable_duplicates": [],
    }
    transcribe = AsyncMock(return_value=transcribe_return)
    if transcribe_error:
        transcribe = AsyncMock(side_effect=transcribe_error)
    dry = AsyncMock(return_value=dry_return)
    if dry_error:
        dry = AsyncMock(side_effect=dry_error)

    with (
        patch("transcription.transcribe_audio", transcribe),
        patch("pipeline.extractor.runner.run_extractor_dry", dry),
        patch("ydoc_store.set_content", AsyncMock()) as set_content,
        EmitRecorder.active() as emit,
    ):
        await widget_extract_task(
            {"job_try": 1}, REF_ID,
            source_doc_id=SRC_DOC, config_doc_id="wex-task-cfg",
            target_doc_id="wex-task-tgt", project_id=PID,
            title_template=None, model=None,
            **(task_kwargs or {}),
        )
    return transcribe, dry, set_content, emit


async def _state():
    from db import fetch_one

    ref = await fetch_one("documents", REF_ID)
    return ref, (ref.get("file_meta") or {}).get("extract") or {}


@pytest.mark.asyncio
async def test_happy_path_writes_statuses_variables_and_nested_queries(test_db):
    await _make_ref(test_db)

    from db import get_db

    proxy = await get_db()
    spy = _QuerySpy()
    undo = spy.install(proxy)
    try:
        _transcribe, dry, set_content, emit = await _run_task()
    finally:
        undo()

    ref, extract = await _state()
    assert extract["status"] == "done"
    assert extract["variables"] == {"Doctor": "House"}
    assert extract["session"] == {"id": "sess-task"}
    assert extract.get("finished_at") is not None
    assert ref["processing_status"] == "processing"  # set_content is mocked in this test

    # The dry extractor got the FULL resolved params, frozen by the route.
    params = dry.call_args.args[0]
    assert (params.reference_id, params.source_doc_id, params.config_doc_id,
            params.target_doc_id, params.project_id) == (
        REF_ID, SRC_DOC, "wex-task-cfg", "wex-task-tgt", PID)

    set_content.assert_awaited_once_with(
        REF_ID, "transcript text", persist=True,
        extra_sets={"processing_status": "ready"},
    )

    emitted = emit.names()
    assert "content_flushed" in emitted
    # WHY-gated: transcription_complete would fire the wet hook → a document.
    assert "transcription_complete" not in emitted
    assert "transcription_error" not in emitted

    # Statuses in order, from the executed UPDATE strings.
    order = ("'transcribing'", "'extracting'", "'done'")
    statuses = [
        st
        for s in spy.sqls if "file_meta.extract.status" in s
        for st in order if f"file_meta.extract.status = {st}" in s
    ]
    assert statuses == list(order)

    # Every write is a NESTED field path — never a whole-object replace (a
    # replace drops session/started_at the upload wrote).
    touching = [s for s in spy.sqls if "file_meta" in s]
    assert touching
    for s in touching:
        assert "SET file_meta =" not in s
        assert "file_meta.extract = " not in s
        assert "file_meta.extract." in s


@pytest.mark.asyncio
async def test_stt_failure_dead_letters_and_keeps_session(test_db):
    await _make_ref(test_db)

    from unittest.mock import AsyncMock, patch

    from jobs.tasks.widget_extract import widget_extract_task

    with (
        patch("transcription.transcribe_audio", AsyncMock(side_effect=RuntimeError("stt down"))),
        patch("ydoc_store.set_content", AsyncMock()) as set_content,
        EmitRecorder.active() as emit,
    ):
        with pytest.raises(RuntimeError, match="stt down"):
            await widget_extract_task(
                {"job_try": 1}, REF_ID,
                source_doc_id=SRC_DOC, config_doc_id="wex-task-cfg",
                target_doc_id="wex-task-tgt", project_id=PID,
            )

    ref, extract = await _state()
    assert extract["status"] == "failed"
    assert "stt down" in extract["error"]
    assert ref["processing_status"] == "error"
    assert extract["session"] == {"id": "sess-task"}  # nested writes never dropped it
    set_content.assert_not_awaited()
    assert "transcription_error" in emit.names()


@pytest.mark.asyncio
async def test_extractor_failure_keeps_transcript_ready(test_db):
    """Extractor failure records the extract error but does not burn the
    SUCCESSFUL transcript's status (plan: 'transcript kept ready')."""
    await _make_ref(test_db)

    from unittest.mock import AsyncMock, patch

    from jobs.tasks.widget_extract import widget_extract_task

    async def fake_set_content(ref_id, text, *, persist=False, extra_sets=None):
        # Minimal stand-in for the real backplane write: land content + the
        # processing_status extra, so the test observes what a real set_content
        # would have persisted.
        from db import get_db

        db = await get_db()
        await db.query(
            "UPDATE type::record('documents', $id) SET content = $c, processing_status = $ps",
            {"id": ref_id, "c": text,
             "ps": (extra_sets or {}).get("processing_status")},
        )

    with (
        patch("transcription.transcribe_audio", AsyncMock(return_value="the transcript")),
        patch("pipeline.extractor.runner.run_extractor_dry",
              AsyncMock(side_effect=ValueError("extraction boom"))),
        patch("ydoc_store.set_content", fake_set_content),
        EmitRecorder.active() as emit,
    ):
        with pytest.raises(ValueError, match="extraction boom"):
            await widget_extract_task(
                {"job_try": 1}, REF_ID,
                source_doc_id=SRC_DOC, config_doc_id="wex-task-cfg",
                target_doc_id="wex-task-tgt", project_id=PID,
            )

    ref, extract = await _state()
    assert extract["status"] == "failed"
    assert "extraction boom" in extract["error"]
    # The transcript landed AND its status was not flipped to error by the recorder.
    assert ref["content"] == "the transcript"
    assert ref["processing_status"] == "ready"
    assert extract["session"] == {"id": "sess-task"}
    emitted = emit.names()
    assert "transcription_error" in emitted
    assert "transcription_complete" not in emitted


def test_worker_settings_widget_extract_max_tries():
    """WorkerSettings registers widget_extract_task on the default worker with
    max_tries=2 (a worker killed mid-task re-queues; max_tries=1 would abort
    WITHOUT running the body — the 2026-08-18 hole)."""
    from jobs.worker import WorkerSettings

    fns = {getattr(f, "name", None): f for f in WorkerSettings.functions}
    assert "widget_extract_task" in fns
    assert fns["widget_extract_task"].max_tries == 2
