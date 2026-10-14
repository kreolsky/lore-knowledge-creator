"""Digest binding: an agent_configs row of kind "digest" — who it reads, and that it never reaches the extractor."""
import pytest

from db import create_record


async def _doc(client, pid: str, token: str, title: str, parent_id: str | None = None) -> str:
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title, "content": "", "parent_id": parent_id},
        cookies={"lore_session": token},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


async def _binding_docs(client, pid: str, token: str) -> tuple[str, str, str]:
    """(theme document, config document, target document)."""
    return (
        await _doc(client, pid, token, "Health"),
        await _doc(client, pid, token, "Health digest config"),
        await _doc(client, pid, token, "Health reports"),
    )


async def _put(client, token: str, theme: str, cfg: str, tgt: str, **extra):
    return await client.put(
        "/api/agent-config",
        json={"document_id": theme, "config_doc_id": cfg, "target_doc_id": tgt, **extra},
        cookies={"lore_session": token},
    )


@pytest.mark.asyncio
async def test_non_root_cannot_name_another_users_chats(client, collab_project):
    pid, _doc_id, admin_token, user_token, admin_uid, _user_uid = collab_project
    theme, cfg, tgt = await _binding_docs(client, pid, admin_token)

    resp = await _put(client, user_token, theme, cfg, tgt, kind="digest", subject_user_ids=[admin_uid])

    assert resp.status_code == 403, resp.text
    assert "project root" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_non_root_digest_reads_only_its_creator(client, collab_project):
    pid, _doc_id, admin_token, user_token, _admin_uid, user_uid = collab_project
    theme, cfg, tgt = await _binding_docs(client, pid, admin_token)

    resp = await _put(client, user_token, theme, cfg, tgt, kind="digest", created_by="someone-else")

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["kind"] == "digest"
    assert row["created_by"] == user_uid
    assert row["subject_user_ids"] == [user_uid]


@pytest.mark.asyncio
async def test_root_may_name_project_members(client, collab_project):
    pid, _doc_id, admin_token, _user_token, admin_uid, user_uid = collab_project
    theme, cfg, tgt = await _binding_docs(client, pid, admin_token)

    resp = await _put(client, admin_token, theme, cfg, tgt, kind="digest",
                      subject_user_ids=[admin_uid, user_uid])

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["created_by"] == admin_uid
    assert sorted(row["subject_user_ids"]) == sorted([admin_uid, user_uid])


@pytest.mark.asyncio
async def test_root_cannot_name_a_user_without_project_access(client, test_db, collab_project):
    pid, _doc_id, admin_token, _user_token, admin_uid, _user_uid = collab_project
    outsider = "test-digest-outsider"
    await test_db.query("DELETE type::record('users', $id)", {"id": outsider})
    await create_record("users", outsider, {
        "name": "outsider", "email": "outsider@test.com", "password_hash": "x",
        "role": "user", "user_facts": "",
    })
    theme, cfg, tgt = await _binding_docs(client, pid, admin_token)

    resp = await _put(client, admin_token, theme, cfg, tgt, kind="digest",
                      subject_user_ids=[admin_uid, outsider])

    assert resp.status_code == 400, resp.text
    await test_db.query("DELETE type::record('users', $id)", {"id": outsider})


@pytest.mark.asyncio
async def test_instance_admin_without_member_row_is_not_root(client, test_db, admin_user, regular_user):
    """A public project gives an instance admin 'full' — but not root, so no override."""
    _admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user
    resp = await client.post("/api/projects", json={"name": "Users Own"}, cookies={"lore_session": user_token})
    assert resp.status_code in (200, 201), resp.text
    pid = resp.json()["project_id"]
    await test_db.query("UPDATE type::record('projects', $id) SET is_public = true", {"id": pid})
    theme, cfg, tgt = await _binding_docs(client, pid, user_token)

    resp = await _put(client, admin_token, theme, cfg, tgt, kind="digest", subject_user_ids=[user_uid])

    assert resp.status_code == 403, resp.text
    assert "project root" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_never_changes_created_by(client, collab_project):
    pid, _doc_id, admin_token, user_token, _admin_uid, user_uid = collab_project
    theme, cfg, tgt = await _binding_docs(client, pid, admin_token)
    created = (await _put(client, user_token, theme, cfg, tgt, kind="digest")).json()
    other_cfg = await _doc(client, pid, admin_token, "Another config")

    resp = await _put(client, admin_token, theme, other_cfg, tgt, kind="digest")

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["config_id"] == created["config_id"]
    assert row["config_doc_id"] == other_cfg
    assert row["created_by"] == user_uid
    assert row["subject_user_ids"] == [user_uid]


@pytest.mark.asyncio
async def test_root_cannot_widen_a_binding_someone_else_created(client, collab_project):
    """Subjects beyond the creator need the CREATOR to be root — the run re-checks exactly that."""
    pid, _doc_id, admin_token, user_token, admin_uid, user_uid = collab_project
    theme, cfg, tgt = await _binding_docs(client, pid, admin_token)
    assert (await _put(client, user_token, theme, cfg, tgt, kind="digest")).status_code == 200

    resp = await _put(client, admin_token, theme, cfg, tgt, kind="digest",
                      subject_user_ids=[admin_uid, user_uid])

    assert resp.status_code == 403, resp.text
    assert "project root" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_non_root_cannot_retarget_a_roots_group_digest(client, collab_project):
    pid, _doc_id, admin_token, user_token, admin_uid, user_uid = collab_project
    theme, cfg, tgt = await _binding_docs(client, pid, admin_token)
    assert (await _put(client, admin_token, theme, cfg, tgt, kind="digest",
                       subject_user_ids=[admin_uid, user_uid])).status_code == 200

    resp = await _put(client, user_token, theme, cfg, tgt, kind="digest", subject_user_ids=[user_uid])

    assert resp.status_code == 403, resp.text
    assert "project root" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_subjects_on_an_extractor_binding_are_refused(client, collab_project):
    pid, _doc_id, admin_token, _user_token, admin_uid, _user_uid = collab_project
    theme, cfg, tgt = await _binding_docs(client, pid, admin_token)

    resp = await _put(client, admin_token, theme, cfg, tgt, subject_user_ids=[admin_uid])

    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_digest_and_extractor_bindings_coexist_on_one_document(client, collab_project):
    pid, _doc_id, admin_token, _user_token, _admin_uid, _user_uid = collab_project
    theme, cfg, tgt = await _binding_docs(client, pid, admin_token)
    extractor_cfg = await _doc(client, pid, admin_token, "Extractor config")

    extractor = (await _put(client, admin_token, theme, extractor_cfg, tgt)).json()
    digest = (await _put(client, admin_token, theme, cfg, tgt, kind="digest")).json()

    assert extractor["config_id"] != digest["config_id"]
    rows = (await client.get(
        f"/api/agent-configs?document_id={theme}", cookies={"lore_session": admin_token},
    )).json()
    assert sorted(r["kind"] for r in rows) == ["digest", "extractor"]


async def _digest_host_with_reference(client, pid: str, token: str) -> str:
    theme, cfg, tgt = await _binding_docs(client, pid, token)
    assert (await _put(client, token, theme, cfg, tgt, kind="digest")).status_code == 200
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "parent_id": theme, "title": "Voice memo",
              "media_type": "markdown", "is_reference": True, "content": "x"},
        cookies={"lore_session": token},
    )
    return resp.json()["document_id"]


@pytest.mark.asyncio
async def test_digest_binding_never_enqueues_the_extractor(client, collab_project, enqueue_recorder):
    pid, _doc_id, admin_token, _user_token, _admin_uid, _user_uid = collab_project
    ref_id = await _digest_host_with_reference(client, pid, admin_token)

    from pipeline.extractor import runner
    await runner.on_transcription_complete(reference_id=ref_id, project_id=pid)

    assert enqueue_recorder.of("extract_task") == []


@pytest.mark.asyncio
async def test_manual_extractor_run_ignores_digest_bindings(client, collab_project, enqueue_recorder):
    pid, _doc_id, admin_token, _user_token, _admin_uid, _user_uid = collab_project
    ref_id = await _digest_host_with_reference(client, pid, admin_token)

    resp = await client.post(
        "/api/agent-config/run", json={"document_id": ref_id}, cookies={"lore_session": admin_token},
    )

    assert resp.status_code == 400, resp.text
    assert enqueue_recorder.of("extract_task") == []


@pytest.mark.asyncio
async def test_digest_only_document_reports_no_extractor(client, collab_project):
    """A digest binding must not light the per-reference "send to agent" button."""
    pid, _doc_id, admin_token, _user_token, _admin_uid, _user_uid = collab_project
    theme, cfg, tgt = await _binding_docs(client, pid, admin_token)
    assert (await _put(client, admin_token, theme, cfg, tgt, kind="digest")).status_code == 200

    single = await client.get(f"/api/agent-config?document_id={theme}", cookies={"lore_session": admin_token})
    listed = await client.get(f"/api/agent-configs?document_id={theme}", cookies={"lore_session": admin_token})

    assert single.status_code == 200 and single.json() is None, single.text
    assert [r["kind"] for r in listed.json()] == ["digest"]


# ─── who may change, run, schedule and delete a digest ──────────────────────
# collab_project: admin = project owner (root), user = full member.


async def _root_digest(client, pid, admin_token) -> tuple[str, tuple[str, str, str]]:
    docs = await _binding_docs(client, pid, admin_token)
    resp = await _put(client, admin_token, *docs, kind="digest")
    assert resp.status_code == 200, resp.text
    return resp.json()["config_id"], docs


@pytest.mark.asyncio
async def test_member_cannot_change_run_or_schedule_another_users_digest(client, collab_project, enqueue_recorder):
    pid, _doc_id, admin_token, user_token, _admin_uid, _user_uid = collab_project
    config_id, (theme, _cfg, tgt) = await _root_digest(client, pid, admin_token)
    other_cfg = await _doc(client, pid, user_token, "Passwords config")
    cookies = {"lore_session": user_token}

    edit = await _put(client, user_token, theme, other_cfg, tgt, kind="digest")
    run = await client.post("/api/agent-config/run", json={"config_id": config_id}, cookies=cookies)
    schedule = await client.post("/api/pipeline-schedules", json={
        "project_id": pid, "pipeline": "digest", "interval_s": 3600,
        "params": {"config_id": config_id, "project_id": pid}}, cookies=cookies)

    assert [edit.status_code, run.status_code, schedule.status_code] == [403, 403, 403], (
        edit.text, run.text, schedule.text)
    assert enqueue_recorder.of("digest_task") == []
    row = (await client.get(f"/api/agent-configs?document_id={theme}", cookies=cookies)).json()[0]
    assert row["config_doc_id"] != other_cfg


@pytest.mark.asyncio
async def test_member_may_delete_another_users_digest(client, collab_project):
    pid, _doc_id, admin_token, user_token, _admin_uid, _user_uid = collab_project
    config_id, (theme, _cfg, _tgt) = await _root_digest(client, pid, admin_token)

    resp = await client.delete(f"/api/agent-config/{config_id}", cookies={"lore_session": user_token})

    assert resp.status_code == 200, resp.text
    listed = await client.get(f"/api/agent-configs?document_id={theme}", cookies={"lore_session": admin_token})
    assert listed.json() == []


@pytest.mark.asyncio
async def test_root_may_change_and_run_a_members_digest(client, collab_project, enqueue_recorder):
    pid, _doc_id, admin_token, user_token, _admin_uid, user_uid = collab_project
    theme, cfg, tgt = await _binding_docs(client, pid, admin_token)
    created = await _put(client, user_token, theme, cfg, tgt, kind="digest")
    assert created.status_code == 200, created.text
    config_id = created.json()["config_id"]

    edit = await _put(client, admin_token, theme, cfg, tgt, kind="digest", title_template="t {yyyy}")
    run = await client.post("/api/agent-config/run", json={"config_id": config_id},
                            cookies={"lore_session": admin_token})

    assert [edit.status_code, run.status_code] == [200, 200], (edit.text, run.text)
    assert edit.json()["created_by"] == user_uid
    assert len(enqueue_recorder.of("digest_task")) == 1
