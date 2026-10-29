"""Tests for the arq extract_task and the on_transcription_complete / manual-run triggers.

Phase C migration: extractor no longer runs as asyncio.create_task with in-process
_inflight_extractions dedup. on_transcription_complete enqueues extract_task via arq;
the manual /agent-config/run route does the same. Dedup is handled by arq job_id.
"""

import asyncio

import pytest


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
