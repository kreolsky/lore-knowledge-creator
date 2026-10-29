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


@pytest.mark.asyncio
async def test_skips_refs_of_deleted_project(test_db):
    """A dead project's refs keep deleted_at = NONE (project delete marks only
    the project row), so recovery must skip them by project liveness."""
    await create_record("projects", "recovery-dead-project", {
        "name": "Recovery Dead", "status": "active",
    })
    await create_record("documents", "recovery-dead-host", {
        "project_id": "recovery-dead-project", "parent_id": None,
        "title": "Host", "content": "", "path": "host.md",
    })
    await create_record("documents", "stuck-deadproj-1", {
        "project_id": "recovery-dead-project", "parent_id": "recovery-dead-host",
        "title": "Ref", "media_type": "audio", "processing_status": "processing",
        "path": "_ref/stuck-deadproj-1.md", "is_reference": True,
    })
    db = await get_db()
    await db.query(
        "UPDATE type::record('projects', $id) SET deleted_at = time::now()",
        {"id": "recovery-dead-project"},
    )
    with EnqueueRecorder.active() as enq:
        await _recover_stuck_transcriptions()
    rows = await db.query("SELECT processing_status FROM type::record('documents', $id)", {"id": "stuck-deadproj-1"})
    assert rows[0]["processing_status"] == "processing"
    assert "transcribe:stuck-deadproj-1" not in _enqueued_job_ids(enq)
