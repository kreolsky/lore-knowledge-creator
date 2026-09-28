"""Tests for the arq extract_task and the on_transcription_complete / manual-run triggers.

Phase C migration: extractor no longer runs as asyncio.create_task with in-process
_inflight_extractions dedup. on_transcription_complete enqueues extract_task via arq;
the manual /agent-config/run route does the same. Dedup is handled by arq job_id.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_on_transcription_complete_enqueues_extract_task(test_db, enqueue_recorder):
    """on_transcription_complete enqueues extract_task with per-config job_id."""
    from pipeline.extractor.runner import on_transcription_complete

    from db import create_record, get_db

    pid = "ext-test-proj-enq"
    doc_id = "ext-test-doc-enq"
    ref_id = "ext-test-ref-enq"
    config_doc_id = "ext-test-cfg-enq"
    target_doc_id = "ext-test-tgt-enq"

    await create_record("documents", doc_id, {
        "project_id": pid, "parent_id": None, "title": "Doc",
        "content": "", "path": f"{doc_id}.md", "is_index": True,
    })
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": doc_id, "title": "Ref",
        "content": "transcribed text", "path": f"_ref/{ref_id}.md",
        "is_reference": True, "media_type": "audio",
    })
    await create_record("documents", config_doc_id, {
        "project_id": pid, "parent_id": None, "title": "Cfg",
        "content": "", "path": "cfg.md", "is_index": False,
    })
    await create_record("documents", target_doc_id, {
        "project_id": pid, "parent_id": None, "title": "Tgt",
        "content": "", "path": "tgt.md", "is_index": False,
    })
    db = await get_db()
    await db.query(
        "CREATE agent_configs CONTENT { document_id: $did, config_doc_id: $cid, "
        "target_doc_id: $tid, project_id: $pid, trigger_event: 'transcription_complete', deleted_at: NONE }",
        {"did": doc_id, "cid": config_doc_id, "tid": target_doc_id, "pid": pid},
    )

    await on_transcription_complete(
        reference_id=ref_id, project_id=pid, user_id="user-1",
    )
    await asyncio.sleep(0.05)

    # Narrow with .of(name) per the enqueue_recorder contract — the raw window
    # can catch a leaked debounce enqueue (embed_document_task) from an
    # earlier test's content_flushed landing late.
    calls = enqueue_recorder.of("extract_task")
    assert len(calls) == 1
    name, args, kwargs = calls[0]
    assert name == "extract_task"
    assert args == (ref_id, doc_id, config_doc_id, target_doc_id, pid)
    assert kwargs["job_id"] == f"extract:{ref_id}:{config_doc_id}:{target_doc_id}"
    assert kwargs.get("title_template") is None
    assert kwargs.get("model") is None
    assert kwargs.get("user_id") == "user-1"


@pytest.mark.asyncio
async def test_on_transcription_complete_no_config_no_enqueue(test_db, enqueue_recorder):
    """on_transcription_complete skips enqueue when no agent_config matches."""
    from pipeline.extractor.runner import on_transcription_complete

    from db import create_record

    pid = "ext-test-proj-no-cfg"
    doc_id = "ext-test-doc-no-cfg"
    ref_id = "ext-test-ref-no-cfg"

    await create_record("documents", doc_id, {
        "project_id": pid, "parent_id": None, "title": "Doc",
        "content": "", "path": f"{doc_id}.md", "is_index": True,
    })
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": doc_id, "title": "Ref",
        "content": "text", "path": f"_ref/{ref_id}.md",
        "is_reference": True, "media_type": "audio",
    })

    await on_transcription_complete(reference_id=ref_id, project_id=pid)
    await asyncio.sleep(0.05)

    assert enqueue_recorder.calls == []


@pytest.mark.asyncio
async def test_manual_run_enqueues_extract_task(client, admin_user, project_with_doc, enqueue_recorder):
    """POST /agent-config/run enqueues extract_task instead of asyncio.create_task."""
    from db import create_record

    pid, idx_id, admin_uid = project_with_doc
    _, admin_token = admin_user

    config_doc_id = "ext-test-cfg-manual"
    target_doc_id = "ext-test-tgt-manual"
    for did in [config_doc_id, target_doc_id]:
        await create_record("documents", did, {
            "project_id": pid, "parent_id": None, "title": f"Doc {did}",
            "content": "", "path": f"{did}.md", "is_index": False,
        })
    await client.put(
        "/api/agent-config",
        json={
            "document_id": idx_id,
            "config_doc_id": config_doc_id,
            "target_doc_id": target_doc_id,
            "trigger_event": "transcription_complete",
        },
        cookies={"lore_session": admin_token},
    )

    ref_id = "ext-test-ref-manual"
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": idx_id, "title": "Manual Ref",
        "content": "text", "path": f"_ref/{ref_id}.md",
        "is_reference": True, "media_type": "markdown",
    })

    resp = await client.post(
        "/api/agent-config/run",
        json={"document_id": ref_id},
        cookies={"lore_session": admin_token},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    # .of(name), not the raw window — a leaked debounce enqueue from an
    # earlier test must not fail this count (enqueue_recorder contract).
    calls = enqueue_recorder.of("extract_task")
    assert len(calls) == 1
    name, args, kwargs = calls[0]
    assert name == "extract_task"
    assert args[0] == ref_id
    assert args[2] == config_doc_id
    assert args[3] == target_doc_id
    assert kwargs["job_id"] == f"extract:{ref_id}:{config_doc_id}:{target_doc_id}"


@pytest.mark.asyncio
async def test_extract_task_success():
    """extract_task delegates to run_extractor on success."""
    from jobs.tasks import extract_task

    with patch("pipeline.extractor.runner.run_extractor", new_callable=AsyncMock) as mock_run:
        await extract_task(
            {"job_try": 1, "max_tries": 4},
            "ref-1", "src-1", "cfg-1", "tgt-1", "proj-1",
            title_template="T", model="m", user_id="u1",
        )

    mock_run.assert_awaited_once_with(
        "ref-1", "src-1", "cfg-1", "tgt-1", "proj-1",
        title_template="T", model="m", user_id="u1",
    )


@pytest.mark.asyncio
async def test_extract_task_dead_letter_creates_error_note():
    """On the final attempt, extract_task calls _create_error_note before re-raising."""
    from jobs.tasks import extract_task

    async def failing_extractor(*a, **kw):
        raise ValueError("pipeline exploded")

    with (
        patch("pipeline.extractor.runner.run_extractor", side_effect=failing_extractor),
        patch("pipeline.extractor.runner._create_error_note", new_callable=AsyncMock) as mock_note,
    ):
        with pytest.raises(ValueError, match="pipeline exploded"):
            await extract_task(
                {"job_try": 4, "max_tries": 4},
                "ref-dl", "src-dl", "cfg-dl", "tgt-dl", "proj-dl",
                user_id="u-dl",
            )

    mock_note.assert_awaited_once()
    call_args = mock_note.call_args
    assert call_args.args[0] == "proj-dl"
    assert call_args.args[1] == "src-dl"
    assert call_args.args[2] == "ref-dl"
    assert isinstance(call_args.args[3], ValueError)
    assert call_args.kwargs.get("config_doc_id") == "cfg-dl"


@pytest.mark.asyncio
async def test_extract_task_dead_letters_on_first_failure():
    """A failure on try 1 still creates the error note — arq does NOT auto-retry plain
    exceptions, so job_try never climbs to max_tries and the failure is terminal.
    Regression for BUG C (2026-06-01): the old job_try>=max_tries gate left no note."""
    from jobs.tasks import extract_task

    async def failing_extractor(*a, **kw):
        raise ValueError("boom on first try")

    with (
        patch("pipeline.extractor.runner.run_extractor", side_effect=failing_extractor),
        patch("pipeline.extractor.runner._create_error_note", new_callable=AsyncMock) as mock_note,
    ):
        with pytest.raises(ValueError, match="boom on first try"):
            await extract_task(
                {"job_try": 1, "max_tries": 4},
                "ref-first", "src-first", "cfg-first", "tgt-first", "proj-first",
            )

    mock_note.assert_awaited_once()
    assert mock_note.call_args.args[2] == "ref-first"


def test_worker_settings_extract_max_tries():
    """WorkerSettings registers extract_task with max_tries=4."""

    from jobs.worker import WorkerSettings

    fns = {getattr(f, "name", None): f for f in WorkerSettings.functions}
    assert "extract_task" in fns
    assert fns["extract_task"].max_tries == 4
