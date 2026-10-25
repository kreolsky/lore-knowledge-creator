"""Digest run: the cursor, whose chats feed it, and what a run writes.

The LLM is faked at its call boundary (the prompt is what the run decided to read);
the embedding provider is faked at its boundary and chats are indexed by the real task.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from helpers import fake_embedding_provider

from jobs.tasks.chat_index import index_chat_messages

_AXES = ("knee", "sleep", "guitar", "tax")
CONFIG = "```yaml\ntheme: health\nkeywords: [knee, sleep]\n```\n\nKeep it short."


def _vec(text: str) -> list[float]:
    low = text.lower()
    return [1.0 if a in low else 0.0 for a in _AXES] + [0.01]


@pytest.fixture
def embed(monkeypatch):
    return fake_embedding_provider(monkeypatch, _vec)


@pytest.fixture
def llm(monkeypatch):
    fake = AsyncMock(return_value={"report": "digest body"})
    monkeypatch.setattr("pipeline.digest.run.call_llm_structured", fake)
    return fake


def _prompt(llm) -> str:
    return llm.call_args.args[0]


async def _doc(client, pid, token, title, content="", parent_id=None) -> str:
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title, "content": content, "parent_id": parent_id},
        cookies={"lore_session": token},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


async def _binding(client, pid, token, title="Health", **extra) -> dict:
    theme = await _doc(client, pid, token, title)
    cfg = await _doc(client, pid, token, f"{title} config", CONFIG)
    target = await _doc(client, pid, token, f"{title} reports")
    resp = await client.put(
        "/api/agent-config",
        json={"document_id": theme, "config_doc_id": cfg, "target_doc_id": target,
              "kind": "digest", **extra},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return {"id": resp.json()["config_id"], "theme": theme, "target": target}


async def _session(db, sid, uid, pid, **fields):
    await db.query(
        "CREATE type::record('chat_sessions', $id) CONTENT $f",
        {"id": sid, "f": {"user_id": uid, "project_id": pid, "title": sid, "model": "m", **fields}},
    )


async def _msg(db, mid, chat_id, content, *, role="user", **fields):
    await db.query(
        "CREATE type::record('messages', $id) CONTENT $f",
        {"id": mid, "f": {"chat_id": chat_id, "role": role, "content": content, **fields}},
    )


async def _reports(db, target) -> list[dict]:
    return await db.query(
        "SELECT * FROM documents WHERE parent_id = $t AND deleted_at IS NONE", {"t": target},
    ) or []


async def _run(binding, pid):
    from pipeline.digest.run import run_digest

    return await run_digest(binding["id"], pid)


async def test_report_written_with_cursor_and_material(client, test_db, collab_project, embed, llm):
    pid, _d, admin_token, _ut, admin_uid, _uu = collab_project
    b = await _binding(client, pid, admin_token)
    await _session(test_db, "anch", admin_uid, pid, document_id=b["theme"])
    await _msg(test_db, "anch0", "anch", "my knee hurts after running")
    await _session(test_db, "other", admin_uid, pid)
    await _msg(test_db, "other0", "other", "slept badly, sleep is broken")
    await _msg(test_db, "other1", "other", "guitar practice notes")
    await index_chat_messages({}, admin_uid, pid)

    result = await _run(b, pid)

    assert result["status"] == "written"
    prompt = _prompt(llm)
    assert "my knee hurts after running" in prompt
    assert "**testadmin:**" in prompt
    assert "sleep is broken" in prompt
    assert "guitar practice" not in prompt
    [report] = await _reports(test_db, b["target"])
    assert report["content"] == "digest body"
    assert report["digest_config_id"] == b["id"]
    assert report["digest_covered_until"] is not None


async def test_second_run_with_nothing_new_writes_nothing(client, test_db, collab_project, embed, llm):
    pid, _d, admin_token, _ut, admin_uid, _uu = collab_project
    b = await _binding(client, pid, admin_token)
    await _session(test_db, "anch", admin_uid, pid, document_id=b["theme"])
    await _msg(test_db, "anch0", "anch", "knee check")
    await index_chat_messages({}, admin_uid, pid)
    assert (await _run(b, pid))["status"] == "written"

    assert (await _run(b, pid)) == {"status": "empty"}
    assert len(await _reports(test_db, b["target"])) == 1


async def test_cursor_is_the_latest_live_report(client, test_db, collab_project, embed, llm):
    pid, _d, admin_token, _ut, admin_uid, _uu = collab_project
    b = await _binding(client, pid, admin_token)
    now = datetime.now(timezone.utc)
    await _session(test_db, "anch", admin_uid, pid, document_id=b["theme"])
    await _msg(test_db, "old", "anch", "knee before the report", created_at=now - timedelta(hours=2))
    await _msg(test_db, "new", "anch", "knee after the report", created_at=now - timedelta(minutes=1))
    await _session(test_db, "other", admin_uid, pid)
    await _msg(test_db, "o-old", "other", "sleep before the report", created_at=now - timedelta(hours=2))
    await _msg(test_db, "o-new", "other", "sleep after the report", created_at=now - timedelta(minutes=1))
    for rid, until, deleted in (("r-old", now - timedelta(days=1), None),
                                ("r-live", now - timedelta(hours=1), None),
                                ("r-dead", now, now)):
        await test_db.query(
            "CREATE type::record('documents', $id) CONTENT $f",
            {"id": rid, "f": {"project_id": pid, "parent_id": b["target"], "title": rid,
                              "path": f"{rid}.md", "content": f"previous {rid}",
                              "digest_config_id": b["id"], "digest_covered_until": until,
                              "deleted_at": deleted}},
        )
    await index_chat_messages({}, admin_uid, pid)

    await _run(b, pid)

    prompt = _prompt(llm)
    assert "knee after the report" in prompt
    assert "knee before the report" not in prompt
    assert "sleep after the report" in prompt
    assert "sleep before the report" not in prompt
    assert "previous r-live" in prompt


async def test_deleted_unindexed_and_note_chats_never_feed_a_digest(
    client, test_db, collab_project, embed, llm,
):
    pid, _d, admin_token, _ut, admin_uid, _uu = collab_project
    b = await _binding(client, pid, admin_token)
    await _session(test_db, "live", admin_uid, pid, document_id=b["theme"])
    await _msg(test_db, "live0", "live", "knee visible")
    for sid, fields in (("gone", {"deleted_at": datetime.now(timezone.utc)}),
                        ("hidden", {"unindexed": True}),
                        ("note", {"is_note": True})):
        await _session(test_db, f"{sid}-a", admin_uid, pid, document_id=b["theme"], **fields)
        await _msg(test_db, f"{sid}-a0", f"{sid}-a", f"knee {sid} anchored")
        await _session(test_db, f"{sid}-o", admin_uid, pid, **fields)
        await _msg(test_db, f"{sid}-o0", f"{sid}-o", f"knee {sid} elsewhere")
    await _msg(test_db, "dead-msg", "live", "knee deleted message",
               deleted_at=datetime.now(timezone.utc))
    await index_chat_messages({}, admin_uid, pid)

    await _run(b, pid)

    prompt = _prompt(llm)
    assert "knee visible" in prompt
    for absent in ("gone", "hidden", "note", "deleted message"):
        assert f"knee {absent}" not in prompt


async def test_only_subjects_chats_are_read(client, test_db, collab_project, embed, llm):
    pid, _d, admin_token, user_token, admin_uid, user_uid = collab_project
    b = await _binding(client, pid, user_token)
    await _session(test_db, "mine", user_uid, pid, document_id=b["theme"])
    await _msg(test_db, "mine0", "mine", "knee of the creator")
    await _session(test_db, "theirs", admin_uid, pid, document_id=b["theme"])
    await _msg(test_db, "theirs0", "theirs", "knee of the root")
    await index_chat_messages({}, admin_uid, pid)
    await index_chat_messages({}, user_uid, pid)

    await _run(b, pid)

    prompt = _prompt(llm)
    assert "knee of the creator" in prompt
    assert "knee of the root" not in prompt


async def test_departed_subject_is_dropped(client, test_db, collab_project, embed, llm):
    pid, _d, admin_token, _ut, admin_uid, user_uid = collab_project
    b = await _binding(client, pid, admin_token, subject_user_ids=[admin_uid, user_uid])
    await _session(test_db, "a", admin_uid, pid, document_id=b["theme"])
    await _msg(test_db, "a0", "a", "knee of the root")
    await _session(test_db, "u", user_uid, pid, document_id=b["theme"])
    await _msg(test_db, "u0", "u", "knee of the member")
    await test_db.query("DELETE project_members WHERE project_id = $pid AND user_id = $uid",
                        {"pid": pid, "uid": user_uid})

    await _run(b, pid)

    prompt = _prompt(llm)
    assert "knee of the root" in prompt
    assert "knee of the member" not in prompt


async def test_only_departed_subject_does_not_fall_back_to_the_creator(
    client, test_db, collab_project, embed, llm,
):
    pid, _d, admin_token, _ut, admin_uid, user_uid = collab_project
    b = await _binding(client, pid, admin_token, subject_user_ids=[user_uid])
    await _session(test_db, "a", admin_uid, pid, document_id=b["theme"])
    await _msg(test_db, "a0", "a", "knee of the root")
    await test_db.query("DELETE project_members WHERE project_id = $pid AND user_id = $uid",
                        {"pid": pid, "uid": user_uid})

    assert (await _run(b, pid)) == {"status": "empty"}
    llm.assert_not_called()


async def test_creator_who_lost_root_fails_with_an_error_note(
    client, test_db, collab_project, embed, llm,
):
    pid, _d, admin_token, _ut, admin_uid, user_uid = collab_project
    b = await _binding(client, pid, admin_token, subject_user_ids=[admin_uid, user_uid])
    await test_db.query(
        "UPDATE type::record('projects', $pid) SET owner_id = $uid; "
        "CREATE project_members SET project_id = $pid, user_id = $admin, access_level = 'full';",
        {"pid": pid, "uid": user_uid, "admin": admin_uid},
    )
    await test_db.query("UPDATE users SET role = 'user' WHERE meta::id(id) = $admin",
                        {"admin": admin_uid})
    from jobs.tasks.digest import digest_task

    with pytest.raises(PermissionError, match="project root"):
        await digest_task({}, b["id"], pid)

    llm.assert_not_called()
    assert await _reports(test_db, b["target"]) == []
    notes = await test_db.query(
        "SELECT meta::id(id) AS id FROM chat_sessions WHERE document_id = $d AND is_note = true",
        {"d": b["theme"]},
    )
    assert len(notes) == 1


async def test_overlapping_themes_both_include_the_same_chat(
    client, test_db, collab_project, embed, llm,
):
    pid, _d, admin_token, _ut, admin_uid, _uu = collab_project
    health = await _binding(client, pid, admin_token, "Health")
    sport = await _binding(client, pid, admin_token, "Sport")
    await _session(test_db, "run", admin_uid, pid)
    await _msg(test_db, "run0", "run", "ran 10k, knee hurts afterwards")
    await index_chat_messages({}, admin_uid, pid)

    await _run(health, pid)
    await _run(sport, pid)

    assert llm.call_count == 2
    assert all("ran 10k, knee hurts" in call.args[0] for call in llm.call_args_list)


async def test_reference_still_processing_holds_the_window(
    client, test_db, collab_project, embed, llm,
):
    pid, _d, admin_token, _ut, admin_uid, _uu = collab_project
    b = await _binding(client, pid, admin_token)
    now = datetime.now(timezone.utc)
    await test_db.query(
        "CREATE type::record('documents', 'voice') CONTENT $f",
        {"f": {"project_id": pid, "parent_id": b["theme"], "title": "voice", "path": "voice.m4a",
               "is_reference": True, "processing_status": "processing", "content": "",
               "created_at": now - timedelta(minutes=10)}},
    )
    await _session(test_db, "anch", admin_uid, pid, document_id=b["theme"])
    await _msg(test_db, "early", "anch", "knee early", created_at=now - timedelta(minutes=20))
    await _msg(test_db, "late", "anch", "knee late", created_at=now - timedelta(minutes=5))

    await _run(b, pid)
    assert "knee late" not in _prompt(llm)

    await test_db.query(
        "UPDATE type::record('documents', 'voice') SET processing_status = 'ready', "
        "content = 'voice transcript about knee'",
    )
    await _run(b, pid)

    prompt = _prompt(llm)
    assert "voice transcript about knee" in prompt
    assert "knee late" in prompt
    assert "knee early" not in prompt


async def test_reference_is_signed_with_its_uploader(client, test_db, collab_project, embed, llm):
    """A voice note names who recorded it, as a chat line names its author — without it
    the model has no name to attribute the facts to."""
    pid, _d, admin_token, _ut, admin_uid, _uu = collab_project
    b = await _binding(client, pid, admin_token)
    await test_db.query(
        "CREATE type::record('documents', 'diary') CONTENT $f",
        {"f": {"project_id": pid, "parent_id": b["theme"], "title": "voice-2026-10-14.webm",
               "path": "diary.webm", "is_reference": True, "processing_status": "ready",
               "content": "slept badly, weak in the evening", "created_by": admin_uid,
               "created_at": datetime(2026, 10, 14, 18, 34, tzinfo=timezone.utc)}},
    )

    await _run(b, pid)

    assert "### voice-2026-10-14.webm — testadmin, 2026-10-14" in _prompt(llm)


async def test_unindexed_message_holds_the_window_until_indexed(
    client, test_db, collab_project, embed, llm,
):
    """A subject's message the chat index has not reached yet closes the window just
    before it: the run must not cover it while semantic search cannot see it, or the
    next run starts past it and it is lost."""
    pid, _d, admin_token, _ut, admin_uid, _uu = collab_project
    b = await _binding(client, pid, admin_token)
    now = datetime.now(timezone.utc)
    await _session(test_db, "anch", admin_uid, pid, document_id=b["theme"])
    await _msg(test_db, "anch0", "anch", "knee on the theme", created_at=now - timedelta(minutes=10))
    await index_chat_messages({}, admin_uid, pid)
    await _session(test_db, "other", admin_uid, pid)
    await _msg(test_db, "late", "other", "sleep was broken again", created_at=now - timedelta(minutes=5))

    await _run(b, pid)

    [report] = await _reports(test_db, b["target"])
    assert report["digest_covered_until"] < now - timedelta(minutes=5)
    assert "sleep was broken again" not in _prompt(llm)

    await index_chat_messages({}, admin_uid, pid)
    await _run(b, pid)

    assert "sleep was broken again" in _prompt(llm)


async def test_long_unindexed_message_does_not_freeze_the_window(
    client, test_db, collab_project, embed, llm,
):
    """A message still unindexed past the hold age (the hourly sweep did not reach it)
    no longer holds the window — one stuck message must not stop the digest."""
    pid, _d, admin_token, _ut, admin_uid, _uu = collab_project
    b = await _binding(client, pid, admin_token)
    now = datetime.now(timezone.utc)
    await _session(test_db, "other", admin_uid, pid)
    await _msg(test_db, "old", "other", "sleep notes", created_at=now - timedelta(hours=3))
    await _session(test_db, "anch", admin_uid, pid, document_id=b["theme"])
    await _msg(test_db, "anch0", "anch", "knee on the theme", created_at=now - timedelta(minutes=1))

    await _run(b, pid)

    [report] = await _reports(test_db, b["target"])
    assert report["digest_covered_until"] > now - timedelta(minutes=1)


async def test_report_under_index_lands_at_root(client, test_db, collab_project, embed, llm):
    """A digest whose target is the project's index writes its report at the
    project root — under the index it would exist but be invisible in the tree."""
    pid, _d, admin_token, _ut, admin_uid, _uu = collab_project
    [project] = await test_db.query(
        "SELECT index_doc_id FROM type::record('projects', $pid)", {"pid": pid},
    )
    index_id = project["index_doc_id"]
    theme = await _doc(client, pid, admin_token, "Health")
    cfg = await _doc(client, pid, admin_token, "Health config", CONFIG)
    resp = await client.put(
        "/api/agent-config",
        json={"document_id": theme, "config_doc_id": cfg, "target_doc_id": index_id,
              "kind": "digest"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    await _session(test_db, "anch", admin_uid, pid, document_id=theme)
    await _msg(test_db, "anch0", "anch", "my knee hurts after running")
    await index_chat_messages({}, admin_uid, pid)

    result = await _run({"id": resp.json()["config_id"]}, pid)

    assert result["status"] == "written"
    assert await _reports(test_db, index_id) == []
    rows = await test_db.query(
        "SELECT * FROM documents WHERE project_id = $pid AND digest_config_id = $cid",
        {"pid": pid, "cid": resp.json()["config_id"]},
    )
    assert len(rows) == 1 and rows[0].get("parent_id") is None
