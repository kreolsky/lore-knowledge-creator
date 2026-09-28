"""Tests for agent config CRUD routes and extractor event integration."""
import pytest


@pytest.mark.asyncio
async def test_get_agent_config_none(client, admin_user, project_with_doc):
    pid, idx_id, admin_uid = project_with_doc
    _, admin_token = admin_user
    resp = await client.get(
        f"/api/agent-config?document_id={idx_id}",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.asyncio
async def test_get_agent_configs_empty(client, admin_user, project_with_doc):
    pid, idx_id, admin_uid = project_with_doc
    _, admin_token = admin_user
    resp = await client.get(
        f"/api/agent-configs?document_id={idx_id}",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_upsert_agent_config(client, admin_user, project_with_doc):
    pid, idx_id, admin_uid = project_with_doc
    _, admin_token = admin_user

    from db import create_record
    config_doc_id = "test-config-doc-ac"
    target_doc_id = "test-target-doc-ac"
    await create_record("documents", config_doc_id, {
        "project_id": pid,
        "parent_id": None,
        "title": "Config Doc",
        "content": "",
        "path": "config_doc.md",
        "is_index": False,
    })
    await create_record("documents", target_doc_id, {
        "project_id": pid,
        "parent_id": None,
        "title": "Target Doc",
        "content": "",
        "path": "target_doc.md",
        "is_index": False,
    })

    resp = await client.put(
        "/api/agent-config",
        json={
            "document_id": idx_id,
            "config_doc_id": config_doc_id,
            "target_doc_id": target_doc_id,
            "trigger_event": "transcription_complete",
        },
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["document_id"] == idx_id
    assert data["config_doc_id"] == config_doc_id
    assert data["target_doc_id"] == target_doc_id
    assert data["project_id"] == pid
    assert data["trigger_event"] == "transcription_complete"
    config_id = data["config_id"]

    get_resp = await client.get(
        f"/api/agent-config?document_id={idx_id}&trigger_event=transcription_complete",
        cookies={"lore_session": admin_token},
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["config_id"] == config_id


@pytest.mark.asyncio
async def test_upsert_agent_config_updates_existing(client, admin_user, project_with_doc):
    pid, idx_id, admin_uid = project_with_doc
    _, admin_token = admin_user

    from db import create_record
    config_doc_id = "test-config-doc-upd"
    config_doc_id_2 = "test-config-doc-upd-2"
    target_doc_id = "test-target-doc-upd"
    for did in [config_doc_id, config_doc_id_2, target_doc_id]:
        await create_record("documents", did, {
            "project_id": pid,
            "parent_id": None,
            "title": f"Doc {did}",
            "content": "",
            "path": f"{did}.md",
            "is_index": False,
        })

    resp1 = await client.put(
        "/api/agent-config",
        json={
            "document_id": idx_id,
            "config_doc_id": config_doc_id,
            "target_doc_id": target_doc_id,
            "trigger_event": "transcription_complete",
        },
        cookies={"lore_session": admin_token},
    )
    assert resp1.status_code == 200
    config_id_1 = resp1.json()["config_id"]

    resp2 = await client.put(
        "/api/agent-config",
        json={
            "document_id": idx_id,
            "config_doc_id": config_doc_id_2,
            "target_doc_id": target_doc_id,
            "trigger_event": "transcription_complete",
        },
        cookies={"lore_session": admin_token},
    )
    assert resp2.status_code == 200
    data = resp2.json()
    assert data["config_id"] == config_id_1
    assert data["config_doc_id"] == config_doc_id_2


@pytest.mark.asyncio
async def test_upsert_multiple_trigger_events(client, admin_user, project_with_doc):
    pid, idx_id, admin_uid = project_with_doc
    _, admin_token = admin_user

    from db import create_record
    config_doc_a = "test-config-doc-multi-a"
    config_doc_b = "test-config-doc-multi-b"
    target_doc = "test-target-doc-multi"
    for did in [config_doc_a, config_doc_b, target_doc]:
        await create_record("documents", did, {
            "project_id": pid,
            "parent_id": None,
            "title": f"Doc {did}",
            "content": "",
            "path": f"{did}.md",
            "is_index": False,
        })

    resp_a = await client.put(
        "/api/agent-config",
        json={
            "document_id": idx_id,
            "config_doc_id": config_doc_a,
            "target_doc_id": target_doc,
            "trigger_event": "transcription_complete",
        },
        cookies={"lore_session": admin_token},
    )
    assert resp_a.status_code == 200

    resp_b = await client.put(
        "/api/agent-config",
        json={
            "document_id": idx_id,
            "config_doc_id": config_doc_b,
            "target_doc_id": target_doc,
            "trigger_event": "custom_event",
        },
        cookies={"lore_session": admin_token},
    )
    assert resp_b.status_code == 200
    assert resp_b.json()["config_id"] != resp_a.json()["config_id"]

    configs_resp = await client.get(
        f"/api/agent-configs?document_id={idx_id}",
        cookies={"lore_session": admin_token},
    )
    assert configs_resp.status_code == 200
    configs = configs_resp.json()
    assert len(configs) == 2
    events = {c["trigger_event"] for c in configs}
    assert events == {"transcription_complete", "custom_event"}


@pytest.mark.asyncio
async def test_delete_agent_config(client, admin_user, project_with_doc):
    pid, idx_id, admin_uid = project_with_doc
    _, admin_token = admin_user

    from db import create_record
    config_doc_id = "test-config-doc-del"
    target_doc_id = "test-target-doc-del"
    await create_record("documents", config_doc_id, {
        "project_id": pid,
        "parent_id": None,
        "title": "Config Doc",
        "content": "",
        "path": "config_del.md",
        "is_index": False,
    })
    await create_record("documents", target_doc_id, {
        "project_id": pid,
        "parent_id": None,
        "title": "Target Doc",
        "content": "",
        "path": "target_del.md",
        "is_index": False,
    })

    resp = await client.put(
        "/api/agent-config",
        json={
            "document_id": idx_id,
            "config_doc_id": config_doc_id,
            "target_doc_id": target_doc_id,
            "trigger_event": "transcription_complete",
        },
        cookies={"lore_session": admin_token},
    )
    config_id = resp.json()["config_id"]

    del_resp = await client.delete(
        f"/api/agent-config/{config_id}",
        cookies={"lore_session": admin_token},
    )
    assert del_resp.status_code == 200

    get_resp = await client.get(
        f"/api/agent-config?document_id={idx_id}&trigger_event=transcription_complete",
        cookies={"lore_session": admin_token},
    )
    assert get_resp.status_code == 200
    assert get_resp.json() is None


@pytest.mark.asyncio
async def test_run_agent_no_config(client, admin_user, project_with_doc):
    pid, idx_id, admin_uid = project_with_doc
    _, admin_token = admin_user

    from db import create_record
    ref_id = "test-ref-no-config"
    await create_record("documents", ref_id, {
        "project_id": pid,
        "parent_id": idx_id,
        "title": "Test Ref",
        "content": "Some transcription text",
        "path": f"_ref/{ref_id}.md",
        "is_index": False,
        "is_reference": True,
        "media_type": "markdown",
    })

    resp = await client.post(
        "/api/agent-config/run",
        json={"document_id": ref_id},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_run_agent_accepted(client, admin_user, project_with_doc):
    pid, idx_id, admin_uid = project_with_doc
    _, admin_token = admin_user

    from db import create_record
    config_doc_id = "test-config-doc-run"
    target_doc_id = "test-target-doc-run"
    values_doc_id = "test-values-doc-run"
    template_doc_id = "test-template-doc-run"

    await create_record("documents", config_doc_id, {
        "project_id": pid,
        "parent_id": None,
        "title": "Config Doc",
        "content": "",
        "path": "config_run.md",
        "is_index": False,
    })
    await create_record("documents", target_doc_id, {
        "project_id": pid,
        "parent_id": None,
        "title": "Target Doc",
        "content": "",
        "path": "target_run.md",
        "is_index": False,
    })
    await create_record("documents", values_doc_id, {
        "project_id": pid,
        "parent_id": config_doc_id,
        "title": "values",
        "content": "```yaml\ncharacter_name: The name of the character\nlocation: Where the scene takes place\n```",
        "path": "values.md",
        "is_index": False,
    })
    await create_record("documents", template_doc_id, {
        "project_id": pid,
        "parent_id": config_doc_id,
        "title": "template",
        "content": "```markdown\n# {{character_name}}\n\nLocation: {{location}}\n```",
        "path": "template.md",
        "is_index": False,
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

    ref_id = "test-ref-run"
    await create_record("documents", ref_id, {
        "project_id": pid,
        "parent_id": idx_id,
        "title": "Test Ref",
        "content": "The character Alice arrived at the Wonderland station.",
        "path": f"_ref/{ref_id}.md",
        "is_index": False,
        "is_reference": True,
        "media_type": "markdown",
    })

    resp = await client.post(
        "/api/agent-config/run",
        json={"document_id": ref_id},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_extractor_event_hook(client, admin_user, project_with_doc):
    pid, idx_id, admin_uid = project_with_doc
    _, admin_token = admin_user

    config_doc_resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Config", "content": "", "parent_id": None},
        cookies={"lore_session": admin_token},
    )
    config_doc_id = config_doc_resp.json()["document_id"]

    target_doc_resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Target", "content": "", "parent_id": None},
        cookies={"lore_session": admin_token},
    )
    target_doc_id = target_doc_resp.json()["document_id"]

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

    ref_resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid,
            "parent_id": idx_id,
            "title": "Hook Ref",
            "media_type": "markdown",
            "is_reference": True,
            "content": "Some text",
        },
        cookies={"lore_session": admin_token},
    )
    ref_id = ref_resp.json()["document_id"]

    from pipeline.extractor.runner import on_transcription_complete
    await on_transcription_complete(reference_id=ref_id, project_id=pid)


@pytest.mark.asyncio
async def test_extractor_dedup_on_duplicate_trigger(client, admin_user, project_with_doc, enqueue_recorder):
    """A doubled transcription_complete must enqueue extract_task only once per config (arq dedup via job_id)."""
    import asyncio

    pid, idx_id, admin_uid = project_with_doc
    _, admin_token = admin_user

    config_doc_id = (await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "DedupCfg", "content": "", "parent_id": None},
        cookies={"lore_session": admin_token},
    )).json()["document_id"]
    target_doc_id = (await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "DedupTgt", "content": "", "parent_id": None},
        cookies={"lore_session": admin_token},
    )).json()["document_id"]
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
    ref_id = (await client.post(
        "/api/documents",
        json={
            "project_id": pid, "parent_id": idx_id, "title": "DedupRef",
            "media_type": "markdown", "is_reference": True, "content": "x",
        },
        cookies={"lore_session": admin_token},
    )).json()["document_id"]

    from pipeline.extractor import runner

    await runner.on_transcription_complete(reference_id=ref_id, project_id=pid)
    await runner.on_transcription_complete(reference_id=ref_id, project_id=pid)
    await asyncio.sleep(0.05)

    # The recorder spans the whole test — the fixtures' doc creations enqueue
    # embed_document_task as a side effect. Narrow to the extractor dispatch.
    calls = enqueue_recorder.of("extract_task")
    assert len(calls) == 2, "Both calls should enqueue (arq dedup handles the no-op)"
    expected_job_id = f"extract:{ref_id}:{config_doc_id}:{target_doc_id}"
    assert all(c[2]["job_id"] == expected_job_id for c in calls)


# ── _fetch_user_timezone ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_user_timezone_none_user_id():
    from pipeline.extractor.runner import _fetch_user_timezone
    result = await _fetch_user_timezone(None)
    assert result is None


@pytest.mark.asyncio
async def test_fetch_user_timezone_system_user_id():
    from pipeline.extractor.runner import _fetch_user_timezone
    result = await _fetch_user_timezone("__recovery__")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_user_timezone_fetch_error():
    from unittest.mock import AsyncMock, patch

    from pipeline.extractor.runner import _fetch_user_timezone
    with patch("pipeline.extractor.runner.fetch_one", new_callable=AsyncMock, side_effect=Exception("db down")):
        result = await _fetch_user_timezone("user-123")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_user_timezone_user_not_found():
    from unittest.mock import AsyncMock, patch

    from pipeline.extractor.runner import _fetch_user_timezone
    with patch("pipeline.extractor.runner.fetch_one", new_callable=AsyncMock, return_value=None):
        result = await _fetch_user_timezone("user-123")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_user_timezone_valid():
    from unittest.mock import AsyncMock, patch

    from pipeline.extractor.runner import _fetch_user_timezone
    with patch("pipeline.extractor.runner.fetch_one", new_callable=AsyncMock, return_value={"timezone": "Europe/Moscow"}):
        result = await _fetch_user_timezone("user-123")
    assert result == "Europe/Moscow"


@pytest.mark.asyncio
async def test_fetch_user_timezone_missing_field():
    from unittest.mock import AsyncMock, patch

    from pipeline.extractor.runner import _fetch_user_timezone
    with patch("pipeline.extractor.runner.fetch_one", new_callable=AsyncMock, return_value={"name": "Alice"}):
        result = await _fetch_user_timezone("user-123")
    assert result is None


# ── _create_error_note: rich message content ────────────────────────────────


@pytest.mark.asyncio
async def test_create_error_note_includes_reference_link():
    from unittest.mock import AsyncMock, patch

    from pipeline.extractor.runner import _create_error_note

    ref_doc = {"title": "Жукоцкая", "is_reference": True}
    config_doc = {"title": "УЗИ конфиг"}

    captured = {}

    async def mock_create_system_note(**kwargs):
        captured.update(kwargs)
        return "note-1"

    with (
        patch("pipeline.extractor.runner.fetch_one", side_effect=[ref_doc, config_doc]),
        patch("notes_service.create_system_note", side_effect=mock_create_system_note),
        patch("pipeline.extractor.runner.emit", new_callable=AsyncMock),
    ):
        await _create_error_note(
            project_id="proj-1",
            document_id="doc-1",
            reference_id="ref-abc",
            error=ValueError("YAML parse error at line 3"),
            config_doc_id="cfg-xyz",
        )

    msg = captured["body"]
    assert "[Жукоцкая](ref:ref-abc)" in msg
    assert "[УЗИ конфиг](doc:cfg-xyz)" in msg
    assert "YAML parse error at line 3" in msg


@pytest.mark.asyncio
async def test_create_error_note_degrades_when_titles_unavailable():
    from unittest.mock import AsyncMock, patch

    from pipeline.extractor.runner import _create_error_note

    captured = {}

    async def mock_create_system_note(**kwargs):
        captured.update(kwargs)
        return "note-1"

    with (
        patch("pipeline.extractor.runner.fetch_one", side_effect=[None, None]),
        patch("notes_service.create_system_note", side_effect=mock_create_system_note),
        patch("pipeline.extractor.runner.emit", new_callable=AsyncMock),
    ):
        await _create_error_note(
            project_id="proj-1",
            document_id="doc-1",
            reference_id="ref-abc",
            error=ValueError("some error"),
            config_doc_id="cfg-xyz",
        )

    msg = captured["body"]
    assert "ref-abc" in msg
    assert "cfg-xyz" in msg


@pytest.mark.asyncio
async def test_extract_task_dead_letter_passes_config_doc_id_to_error_note():
    from unittest.mock import AsyncMock, patch

    from jobs.tasks import extract_task

    async def failing_extractor(*args, **kwargs):
        raise ValueError("boom")

    with (
        patch("pipeline.extractor.runner.run_extractor", side_effect=failing_extractor),
        patch("pipeline.extractor.runner._create_error_note", new_callable=AsyncMock) as mock_note,
    ):
        with pytest.raises(ValueError, match="boom"):
            await extract_task(
                {"job_try": 4, "max_tries": 4},
                reference_id="ref-1",
                source_doc_id="src-1",
                config_doc_id="cfg-99",
                target_doc_id="tgt-1",
                project_id="proj-1",
            )

    mock_note.assert_awaited_once()
    call_args = mock_note.call_args
    assert call_args.kwargs.get("config_doc_id") == "cfg-99" or "cfg-99" in str(call_args)


@pytest.mark.asyncio
async def test_run_extractor_emits_sort_key(project_with_doc):
    """Regression: run_extractor's document_created event MUST carry sort_key.

    Without it the client stores the new doc keyless and a drag-reorder silently
    reverts until a page reload re-fetches the real key (incident 2026-06-04).
    """
    from unittest.mock import AsyncMock, patch

    from pipeline.extractor import runner

    pid, target_id, _ = project_with_doc

    async def fake_run_async(shared):
        shared["rendered_markdown"] = "# Extracted\n\nbody"

    flow = AsyncMock()
    flow.run_async.side_effect = fake_run_async

    captured = []
    async def fake_emit(event_type, **kwargs):
        captured.append((event_type, kwargs))

    with patch("pipeline.extractor.runner.create_extractor_flow", return_value=flow), \
         patch("pipeline.extractor.runner.emit", side_effect=fake_emit):
        await runner.run_extractor(
            reference_id="ref-x",
            source_doc_id="src-x",
            config_doc_id="cfg-x",
            target_doc_id=target_id,
            project_id=pid,
        )

    created = [kw for ev, kw in captured if ev == "document_created"]
    assert created, f"document_created not emitted; got {[e for e, _ in captured]}"
    assert created[0].get("sort_key"), "document_created emit is missing sort_key"
