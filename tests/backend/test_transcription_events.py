"""Tests for the arq transcribe_task and the enqueue_transcription trigger.

Phase B migration: transcription no longer runs on an in-process per-user
asyncio worker. enqueue_transcription submits an arq job; transcribe_task (in
jobs/tasks.py) runs in the worker process and reaches editors via set_content +
the Redis backplane (it has no live collab session).
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


async def _create_audio_ref(ref_id: str, pid: str) -> None:
    from db import create_record
    await create_record("documents", ref_id, {
        "project_id": pid,
        "parent_id": "some-doc",
        "title": "audio.webm",
        "media_type": "audio",
        "content": "",
        "processing_status": "queued",
        "file_path": "fake/path/audio.webm",
        "path": f"_ref/{ref_id}.md",
        "is_reference": True,
    })


@pytest.mark.asyncio
async def test_enqueue_transcription_submits_arq_job(enqueue_recorder):
    """enqueue_transcription enqueues transcribe_task with a per-ref job_id on the transcription queue."""
    import transcription

    await transcription.enqueue_transcription("user-1", "ref-xyz")

    assert enqueue_recorder.calls == [(
        "transcribe_task",
        ("user-1", "ref-xyz"),
        {"job_id": "transcribe:ref-xyz", "queue": "transcription"},
    )]


@pytest.mark.asyncio
async def test_transcribe_task_complete_emits_event(test_db):
    """Successful transcribe_task sets content via set_content and emits transcription_complete."""
    from event_bus import off, on
    from jobs.tasks import transcribe_task

    ref_id = "task-test-ref-ok"
    pid = "task-test-project"
    await _create_audio_ref(ref_id, pid)

    events = []
    async def capture(**kw):
        events.append(kw)

    on("transcription_complete", capture)
    try:
        with patch("transcription.transcribe_audio", new_callable=AsyncMock, return_value="Hello world"), \
             patch("ydoc_store.set_content", new_callable=AsyncMock) as set_content, \
             patch("jobs.tasks.media.STORAGE_PATH"):
            await transcribe_task({"job_try": 1, "max_tries": 2}, "test-user", ref_id)
            # emit() is fire-and-forget (create_task) — drain the loop so the
            # local capture handler runs before asserting.
            await asyncio.sleep(0.05)

        # INVARIANT: worker reaches editors via set_content (no live session), not
        # apply_external_content_change.
        set_content.assert_awaited_once()
        assert set_content.await_args.args[1] == "Hello world"
        assert len(events) == 1
        assert events[0]["reference_id"] == ref_id
        assert events[0]["project_id"] == pid
    finally:
        off("transcription_complete", capture)


@pytest.mark.asyncio
async def test_transcribe_task_emits_content_flushed_to_schedule_embed(test_db):
    """A worker-written content change MUST emit content_flushed so the embedding
    scheduler (embeddings._on_content_flushed) enqueues an embed_document_task.

    Before D1b the worker wrote via set_content, which does NOT emit
    content_flushed — so a transcription was persisted and broadcast to editors
    but NEVER embedded (the scheduler had nothing to subscribe to on the worker
    process). With D1 (eager embeddings import) the scheduler IS subscribed; this
    is the second half: the write site must emit the event it listens for.
    """
    from event_bus import off, on
    from jobs.tasks import transcribe_task

    ref_id = "task-test-ref-flush"
    pid = "task-test-project-flush"
    await _create_audio_ref(ref_id, pid)

    flushed = []
    async def capture(**kw):
        flushed.append(kw)

    on("content_flushed", capture)
    try:
        with patch("transcription.transcribe_audio", new_callable=AsyncMock, return_value="Hello world"), \
             patch("ydoc_store.set_content", new_callable=AsyncMock), \
             patch("jobs.tasks.media.STORAGE_PATH"):
            await transcribe_task({"job_try": 1, "max_tries": 2}, "test-user", ref_id)
            await asyncio.sleep(0.05)  # drain fire-and-forget emit

        assert len(flushed) == 1, f"expected one content_flushed, got {flushed}"
        assert flushed[0]["entity_type"] == "doc"
        assert flushed[0]["entity_id"] == ref_id
        assert flushed[0]["project_id"] == pid
    finally:
        off("content_flushed", capture)


@pytest.mark.asyncio
async def test_transcribe_task_dead_letter_sets_error(test_db):
    """On the final attempt, a failed transcription sets status 'error' and emits transcription_error."""
    from db import get_db
    from event_bus import off, on
    from jobs.tasks import transcribe_task

    ref_id = "task-test-ref-err"
    pid = "task-test-project-err"
    await _create_audio_ref(ref_id, pid)

    events = []
    async def capture(**kw):
        events.append(kw)

    on("transcription_error", capture)
    try:
        with patch("transcription.transcribe_audio", new_callable=AsyncMock, side_effect=RuntimeError("STT failed")), \
             patch("jobs.tasks.media.STORAGE_PATH"):
            with pytest.raises(RuntimeError):
                await transcribe_task({"job_try": 2, "max_tries": 2}, "test-user", ref_id)
            await asyncio.sleep(0.05)  # drain fire-and-forget emit

        assert len(events) == 1
        assert events[0]["reference_id"] == ref_id
        db = await get_db()
        rows = await db.query(
            "SELECT processing_status FROM type::record('documents', $id)", {"id": ref_id}
        )
        assert rows[0]["processing_status"] == "error"
    finally:
        off("transcription_error", capture)


@pytest.mark.asyncio
async def test_transcribe_task_dead_letters_on_first_failure(test_db):
    """A failure on try 1 already sets status 'error' and emits transcription_error.

    arq does NOT auto-retry plain exceptions, so job_try never climbs to max_tries —
    the failure is terminal. Regression for BUG C (2026-06-01): the old
    job_try>=max_tries gate (1 >= 2 → false) left failed refs stuck in 'processing'.
    """
    from db import get_db
    from event_bus import off, on
    from jobs.tasks import transcribe_task

    ref_id = "task-test-ref-first"
    pid = "task-test-project-first"
    await _create_audio_ref(ref_id, pid)

    events = []
    async def capture(**kw):
        events.append(kw)

    on("transcription_error", capture)
    try:
        with patch("transcription.transcribe_audio", new_callable=AsyncMock, side_effect=RuntimeError("STT failed")), \
             patch("jobs.tasks.media.STORAGE_PATH"):
            with pytest.raises(RuntimeError):
                await transcribe_task({"job_try": 1, "max_tries": 2}, "test-user", ref_id)
            await asyncio.sleep(0.05)  # drain fire-and-forget emit

        assert len(events) == 1
        assert events[0]["reference_id"] == ref_id
        db = await get_db()
        rows = await db.query(
            "SELECT processing_status FROM type::record('documents', $id)", {"id": ref_id}
        )
        assert rows[0]["processing_status"] == "error"
    finally:
        off("transcription_error", capture)
