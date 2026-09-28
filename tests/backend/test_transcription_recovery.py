"""Tests for stuck transcription recovery on startup.

Phase B migration: recovery moved from transcription.recover_stuck_transcriptions
to main._recover_stuck_transcriptions, which sets stuck refs back to 'queued' and
re-enqueues an arq transcribe_task (job_id dedup makes it idempotent per restart).
"""


import pytest
from enqueue_recorder import EnqueueRecorder

from db import create_record, get_db
from main import _recover_stuck_transcriptions


def _enqueued_job_ids(enq) -> list[str]:
    """job_ids enqueued across all awaits — recovery scans the whole table, and
    the session DB accumulates refs from earlier tests, so scope to the job_id."""
    return [c.kwargs.get("job_id") for c in enq.calls]


async def _create_test_ref(ref_id: str, status: str, deleted: bool = False) -> None:
    """Helper: create a reference-document with given processing_status in test DB."""
    db = await get_db()
    try:
        await create_record("projects", "recovery-test-project", {
            "name": "Recovery Test", "status": "active",
        })
    except RuntimeError:
        pass
    # Reference-host invariant: attach every test ref to a real host doc (idempotent).
    host_id = "recovery-test-host"
    try:
        await create_record("documents", host_id, {
            "project_id": "recovery-test-project", "parent_id": None,
            "title": "Host", "content": "", "path": "host.md",
        })
    except RuntimeError:
        pass
    await create_record("documents", ref_id, {
        "project_id": "recovery-test-project",
        "parent_id": host_id,
        "title": f"Test Ref {ref_id}",
        "media_type": "audio",
        "processing_status": status,
        "path": f"_ref/{ref_id}.md",
        "is_reference": True,
    })
    if deleted:
        await db.query(
            "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
            {"id": ref_id},
        )


@pytest.mark.asyncio
async def test_recovers_processing_refs(test_db):
    await _create_test_ref("stuck-proc-1", "processing")
    with EnqueueRecorder.active() as enq:
        await _recover_stuck_transcriptions()
    db = await get_db()
    rows = await db.query("SELECT processing_status FROM type::record('documents', $id)", {"id": "stuck-proc-1"})
    assert rows[0]["processing_status"] == "queued"
    assert "transcribe:stuck-proc-1" in _enqueued_job_ids(enq)
    call = next(c for c in enq.calls if c.kwargs.get("job_id") == "transcribe:stuck-proc-1")
    assert call.name == "transcribe_task"
    assert call.args == ("__recovery__", "stuck-proc-1")
    assert call.kwargs == {"job_id": "transcribe:stuck-proc-1", "queue": "transcription"}


@pytest.mark.asyncio
async def test_recovers_queued_refs(test_db):
    await _create_test_ref("stuck-queue-1", "queued")
    with EnqueueRecorder.active() as enq:
        await _recover_stuck_transcriptions()
    db = await get_db()
    rows = await db.query("SELECT processing_status FROM type::record('documents', $id)", {"id": "stuck-queue-1"})
    assert rows[0]["processing_status"] == "queued"
    assert "transcribe:stuck-queue-1" in _enqueued_job_ids(enq)


@pytest.mark.asyncio
async def test_skips_deleted_refs(test_db):
    await _create_test_ref("stuck-del-1", "processing", deleted=True)
    with EnqueueRecorder.active() as enq:
        await _recover_stuck_transcriptions()
    db = await get_db()
    rows = await db.query("SELECT processing_status FROM type::record('documents', $id)", {"id": "stuck-del-1"})
    assert rows[0]["processing_status"] == "processing"
    assert "transcribe:stuck-del-1" not in _enqueued_job_ids(enq)


@pytest.mark.asyncio
async def test_skips_ready_refs(test_db):
    await _create_test_ref("stuck-ready-1", "ready")
    with EnqueueRecorder.active() as enq:
        await _recover_stuck_transcriptions()
    db = await get_db()
    rows = await db.query("SELECT processing_status FROM type::record('documents', $id)", {"id": "stuck-ready-1"})
    assert rows[0]["processing_status"] == "ready"
    assert "transcribe:stuck-ready-1" not in _enqueued_job_ids(enq)


@pytest.mark.asyncio
async def test_handles_empty_result(test_db):
    with EnqueueRecorder.active():
        await _recover_stuck_transcriptions()
