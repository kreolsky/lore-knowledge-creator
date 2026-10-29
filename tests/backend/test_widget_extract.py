"""Widget extract batch API — POST /api/widget/extract + GET /api/widget/extract/{job_id}.

Plan widget-extract-batch-api (CIR T2): a clinic-side utility POSTs one recording
under a widget API key and receives config variables as JSON, with NO document
created. job_id IS the reference id; job state lives in file_meta.extract.
"""

import io

import pytest

WEBM_MAGIC = b"\x1aE\xdf\xa3" + b"\x00" * 96


async def _sandbox_doc(client, admin_token, project_id, title="WidgetExtractSandbox"):
    resp = await client.post(
        "/api/documents",
        json={"project_id": project_id, "title": title},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _widget_key(client, admin_token, doc_id, capabilities=("widget",)):
    resp = await client.post(
        "/api/api-keys",
        json={
            "document_id": doc_id, "label": "wex-test",
            "capabilities": list(capabilities),
        },
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def _add_agent_config(client, admin_token, project_id, doc_id,
                            trigger_event="transcription_complete"):
    """Create config/target docs and bind ONE agent_configs row on the sandbox doc."""
    ids = {}
    for role in ("cfg", "tgt"):
        resp = await client.post(
            "/api/documents",
            json={"project_id": project_id, "title": f"wex-{role}"},
            cookies={"lore_session": admin_token},
        )
        ids[role] = resp.json()["document_id"]
    resp = await client.put(
        "/api/agent-config",
        json={
            "document_id": doc_id,
            "config_doc_id": ids["cfg"],
            "target_doc_id": ids["tgt"],
            "trigger_event": trigger_event,
        },
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    return ids["cfg"], ids["tgt"]


async def _post_extract(client, token, session_json,
                        filename="rec.webm", mime="audio/webm", content=WEBM_MAGIC):
    return await client.post(
        "/api/widget/extract",
        files={"file": (filename, io.BytesIO(content), mime)},
        data={"session": session_json},
        headers={"Authorization": f"Bearer {token}"},
    )


async def _setup(client, admin_token, project_id, *,
                 with_config=True, second_config=False, title="WidgetExtractSandbox"):
    doc_id = await _sandbox_doc(client, admin_token, project_id, title=title)
    if with_config:
        await _add_agent_config(client, admin_token, project_id, doc_id)
    if second_config:
        await _add_agent_config(client, admin_token, project_id, doc_id,
                                trigger_event="manual")
    token = await _widget_key(client, admin_token, doc_id)
    return doc_id, token


async def _extract_state(ref_id):
    from db import fetch_one

    ref = await fetch_one("documents", ref_id)
    return (ref.get("file_meta") or {}).get("extract") or {}


# ─── (b2) the concurrent race loser gets the winner's job, not a 500 ─────────


@pytest.mark.asyncio
async def test_race_loser_receives_existing_job(
    client, admin_user, project_with_doc, enqueue_recorder,
):
    """save_audio_upload raises while the winner's row exists (unique-index race):
    the catch-arm re-select serves the existing job_id."""
    from unittest.mock import AsyncMock, patch

    from documents.service import find_reference_by_idempotency_key

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, token = await _setup(client, admin_token, pid)

    first = await _post_extract(client, token, '{"id": "sess-race"}')
    assert first.status_code == 200
    job_id = first.json()["job_id"]

    calls = {"n": 0}

    async def lookup_missing_first(project_id, key):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the pre-check cannot see the winner's uncommitted row
        return await find_reference_by_idempotency_key(project_id, key)

    with (
        patch("routes.widget.find_reference_by_idempotency_key", lookup_missing_first),
        patch("routes.widget.save_audio_upload",
              AsyncMock(side_effect=RuntimeError("unique-index race loser"))),
    ):
        resp = await _post_extract(client, token, '{"id": "sess-race"}')

    assert resp.status_code == 200, resp.text
    assert resp.json()["job_id"] == job_id


# ─── (b3) a failed job is reset and re-enqueued on replay ────────────────────


@pytest.mark.asyncio
async def test_replay_failed_resets_and_reenqueues(
    client, admin_user, project_with_doc, enqueue_recorder,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, token = await _setup(client, admin_token, pid)

    resp = await _post_extract(client, token, '{"id": "sess-f"}')
    job_id = resp.json()["job_id"]

    from db import get_db

    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET "
        "file_meta.extract.status = 'failed', file_meta.extract.error = 'stt outage'",
        {"id": job_id},
    )

    again = await _post_extract(client, token, '{"id": "sess-f"}')
    assert again.status_code == 200, again.text
    assert again.json() == {"job_id": job_id, "status": "queued"}

    state = await _extract_state(job_id)
    assert state["status"] == "queued"
    assert state.get("error") is None  # reset clears the stale error text
    assert state["session"]["id"] == "sess-f"  # nested writes never dropped it
    from datetime import datetime

    assert isinstance(state["started_at"], datetime)  # same type as the init write
    calls = enqueue_recorder.of("widget_extract_task")
    assert len(calls) == 2
    assert calls[1].kwargs["job_id"] == f"widget-extract:{job_id}"


# ─── (c) session validation ───────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("session_json", [
    "",                       # absent form field (default "")
    '{"id": ""}',             # blank id
    '{"id": "   "}',          # whitespace-only id
    "{}",                     # missing id
    "not-json",
    '"just-a-string"',
])
async def test_bad_session_rejected_400(
    client, admin_user, project_with_doc, session_json, enqueue_recorder,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id, token = await _setup(client, admin_token, pid)

    resp = await _post_extract(client, token, session_json)
    assert resp.status_code == 400, resp.text
    assert enqueue_recorder.calls == []


# ─── (f)/(g) GET status ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_other_documents_reference_403(
    client, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_a, token_a = await _setup(client, admin_token, pid)
    doc_b, token_b = await _setup(client, admin_token, pid, title="WidgetExtractOther")

    mine = await _post_extract(client, token_a, '{"id": "sess-ga"}')
    foreign = await _post_extract(client, token_b, '{"id": "sess-gb"}')
    assert mine.status_code == foreign.status_code == 200

    resp = await client.get(
        f"/api/widget/extract/{mine.json()['job_id']}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 403

    own = await client.get(
        f"/api/widget/extract/{mine.json()['job_id']}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert own.status_code == 200


# ─── (h) the refused principal: a key without the widget capability ──────────


@pytest.mark.asyncio
async def test_key_without_widget_capability_403(
    client, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _sandbox_doc(client, admin_token, pid)
    agent_only_token = await _widget_key(
        client, admin_token, doc_id, capabilities=("agent",),
    )

    resp = await _post_extract(client, agent_only_token, '{"id": "sess-h"}')
    assert resp.status_code == 403
