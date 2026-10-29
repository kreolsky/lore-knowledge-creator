"""Digest triggers: a manual run, a finished transcription and a schedule each enqueue one digest_task."""
import json

import pytest

from db import create_record

DIGEST_JOB = "digest_task"
PIPELINE_JOBS = (DIGEST_JOB, "extract_task")


def _pipeline_calls(recorder) -> list:
    """Digest/extractor postings only — document creation also posts embed jobs."""
    return [c for c in recorder.calls if c.name in PIPELINE_JOBS]


async def _doc(client, pid: str, token: str, title: str, parent_id: str | None = None, **extra) -> str:
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title, "content": "x", "parent_id": parent_id, **extra},
        cookies={"lore_session": token},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


async def _bind(client, pid: str, token: str, kind: str = "digest") -> tuple[str, str]:
    """(config_id, theme document id) of a fresh binding of `kind`."""
    theme = await _doc(client, pid, token, "Health")
    cfg = await _doc(client, pid, token, "Health config")
    target = await _doc(client, pid, token, "Health reports")
    resp = await client.put(
        "/api/agent-config",
        json={"document_id": theme, "config_doc_id": cfg, "target_doc_id": target, "kind": kind},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["config_id"], theme


def _assert_one_digest_run(recorder, config_id: str, pid: str) -> None:
    calls = recorder.of(DIGEST_JOB)
    assert len(calls) == 1, recorder.calls
    assert calls[0].kwargs["config_id"] == config_id
    assert calls[0].kwargs["project_id"] == pid
    assert calls[0].kwargs["job_id"] == f"digest:{config_id}"
    assert recorder.of("extract_task") == []


# ─── manual run ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_run_by_config_id_enqueues_one_digest(client, collab_project, enqueue_recorder):
    pid, _doc_id, admin_token, _user_token, _admin_uid, _user_uid = collab_project
    config_id, _theme = await _bind(client, pid, admin_token)

    resp = await client.post(
        "/api/agent-config/run", json={"config_id": config_id}, cookies={"lore_session": admin_token},
    )

    assert resp.status_code == 200, resp.text
    _assert_one_digest_run(enqueue_recorder, config_id, pid)


@pytest.mark.asyncio
async def test_manual_run_refuses_a_user_without_project_access(
    client, project_with_doc, regular_user, enqueue_recorder,
):
    pid, _idx, _uid = project_with_doc
    await create_record("agent_configs", "dg-foreign", {
        "document_id": "d", "config_doc_id": "c", "target_doc_id": "t",
        "project_id": pid, "kind": "digest", "trigger_event": "transcription_complete",
        "created_by": "someone",
    })

    resp = await client.post(
        "/api/agent-config/run", json={"config_id": "dg-foreign"},
        cookies={"lore_session": regular_user[1]},
    )

    # A non-member is told the project does not exist (no existence oracle).
    assert resp.status_code == 404, resp.text
    assert _pipeline_calls(enqueue_recorder) == []


# ─── transcription_complete ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transcription_of_an_attached_reference_runs_the_digest(client, collab_project, enqueue_recorder):
    pid, _doc_id, admin_token, _user_token, _admin_uid, _user_uid = collab_project
    config_id, theme = await _bind(client, pid, admin_token)
    ref_id = await _doc(client, pid, admin_token, "Voice memo", theme, media_type="markdown", is_reference=True)

    from pipeline.digest import triggers
    await triggers.on_transcription_complete(reference_id=ref_id, project_id=pid)

    _assert_one_digest_run(enqueue_recorder, config_id, pid)


# ─── schedules ───────────────────────────────────────────────────────────────


async def _schedule(client, token: str, pid: str, config_id: str):
    return await client.post(
        "/api/pipeline-schedules",
        json={"project_id": pid, "pipeline": "digest", "interval_s": 3600,
              "params": {"config_id": config_id, "project_id": pid}},
        cookies={"lore_session": token},
    )


@pytest.mark.asyncio
async def test_scheduled_digest_ticks_into_one_digest_run(client, collab_project, enqueue_recorder):
    pid, _doc_id, admin_token, _user_token, _admin_uid, _user_uid = collab_project
    config_id, _theme = await _bind(client, pid, admin_token)
    resp = await _schedule(client, admin_token, pid, config_id)
    assert resp.status_code == 200, resp.text

    from jobs import tasks
    await tasks.pipeline_schedule_tick_task({"redis": None})

    _assert_one_digest_run(enqueue_recorder, config_id, pid)


@pytest.mark.asyncio
async def test_schedule_for_a_binding_of_another_project_is_refused(
    client, collab_project, enqueue_recorder,
):
    pid, _doc_id, admin_token, _user_token, _admin_uid, _user_uid = collab_project
    await create_record("agent_configs", "dg-other-project", {
        "document_id": "d", "config_doc_id": "c", "target_doc_id": "t",
        "project_id": "other-project", "kind": "digest", "trigger_event": "transcription_complete",
        "created_by": "someone",
    })

    resp = await _schedule(client, admin_token, pid, "dg-other-project")

    assert resp.status_code == 400, resp.text
    assert "config_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_tick_refuses_a_binding_of_another_project(client, collab_project, enqueue_recorder):
    """Defense in depth behind create validation: a hand-written row is refused at claim."""
    pid, _doc_id, _admin_token, _user_token, admin_uid, _user_uid = collab_project
    await create_record("agent_configs", "dg-other-project", {
        "document_id": "d", "config_doc_id": "c", "target_doc_id": "t",
        "project_id": "other-project", "kind": "digest", "trigger_event": "transcription_complete",
        "created_by": "someone",
    })
    await create_record("pipeline_schedules", "ps-dg-cross", {
        "project_id": pid, "pipeline": "digest", "interval_s": 60, "enabled": True,
        "params_json": json.dumps({"config_id": "dg-other-project", "project_id": pid}),
        "created_by": admin_uid,
    })

    from db import fetch_one
    from jobs import tasks
    await tasks.pipeline_schedule_tick_task({"redis": None})

    assert _pipeline_calls(enqueue_recorder) == []
    row = await fetch_one("pipeline_schedules", "ps-dg-cross")
    assert row["last_status"] == "error"
    assert "config_id" in row["last_error"]
