"""The agent's five pipeline tools (plan pipeline-agent-tools): the key's walls
and the run-log lifecycle across the web → arq → worker → DB crossing.

The refused-principal test drives the principals that must be REFUSED: a key of
project A naming project B's binding or document while the owning user holds
full access in BOTH (RBAC alone passes — only the key's project can refuse), a
subtree key naming a document outside its subtree, a full member who did not
create the digest, an unconfirmed call (409, nothing written), and the admin's
skill tombstone. The lifecycle test drives the real surface end to end: the
tool, the real arq pool (the job_id collapse is Redis's answer, not a fake's),
the worker task flipping the run row, the tool reading it back.
"""
import pytest
from helpers import fake_embedding_provider

from db import fetch_one

CONFIG = "```yaml\ntheme: health\nkeywords: [knee]\n```\n\nKeep it short."


@pytest.fixture
def embed(monkeypatch):
    """The digest's embedding boundary — `collect` embeds its query even when
    the window turns out to hold nothing."""
    return fake_embedding_provider(monkeypatch, lambda _text: [1.0])


async def _doc(client, token, pid, title, parent_id=None, **extra) -> str:
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title, "content": "x", "parent_id": parent_id, **extra},
        cookies={"lore_session": token},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


async def _bind(client, token, pid, label, kind="digest") -> dict:
    """A fresh binding of `kind` on three new documents of `pid`."""
    theme = await _doc(client, token, pid, f"{label} theme")
    cfg = await _doc(client, token, pid, f"{label} config", content=CONFIG)
    tgt = await _doc(client, token, pid, f"{label} reports")
    resp = await client.put(
        "/api/agent-config",
        json={"document_id": theme, "config_doc_id": cfg, "target_doc_id": tgt, "kind": kind},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return {"id": resp.json()["config_id"], "theme": theme, "cfg": cfg, "tgt": tgt}


async def _key(uid: str, pid: str, **extra) -> dict:
    """Authorization headers of a fresh agent key of `pid` owned by `uid`."""
    from agent.keys import create_agent_key_row

    token, _, _ = await create_agent_key_row(uid, pid, **extra)
    return {"Authorization": f"Bearer {token}"}


async def _tool(client, name: str, headers: dict, **body):
    return await client.post(f"/api/tool/{name}", json=body, headers=headers)


# TEST-WHY: refused-principal — a project-A key names project B's binding/document while its owner holds full access in both; a subtree key names a document outside its subtree; a non-creator runs a digest; an unconfirmed call writes nothing; the skill tombstone closes all five tools.
async def test_pipeline_tools_refuse_principals(client, test_db, collab_project):
    pid, _d, admin_token, user_token, admin_uid, user_uid = collab_project
    # Project B, owned by the SAME admin: the key's user holds full access in
    # both projects, so every refusal below is the key's wall, never RBAC's.
    pid_b = (await client.post(
        "/api/projects", json={"name": "Other Project"}, cookies={"lore_session": admin_token},
    )).json()["project_id"]
    await client.post(
        f"/api/admin/projects/{pid_b}/members",
        json={"user_id": user_uid, "access_level": "full"}, cookies={"lore_session": admin_token},
    )
    digest_b = await _bind(client, admin_token, pid_b, "Foreign digest")
    extractor_b = await _bind(client, admin_token, pid_b, "Foreign extract", kind="extractor")
    ref_b = await _doc(client, admin_token, pid_b, "Foreign ref",
                       parent_id=extractor_b["theme"], media_type="markdown", is_reference=True)
    key_a = await _key(admin_uid, pid)

    # A project-A key addresses project B by id: uniform 404, on every tool
    # that takes a binding id or a document id — and B is left untouched.
    cases = [
        ("run_pipeline", {"config_id": digest_b["id"], "apply": "auto"}, "Agent config not found"),
        ("unbind_pipeline", {"config_id": digest_b["id"], "apply": "auto"}, "Agent config not found"),
        ("pipeline_runs", {"config_id": digest_b["id"]}, "Agent config not found"),
        ("run_pipeline", {"config_id": extractor_b["id"], "reference_id": ref_b, "apply": "auto"},
         "Agent config not found"),
        ("bind_pipeline", {"document_id": digest_b["theme"], "config_doc_id": digest_b["cfg"],
                           "target_doc_id": digest_b["tgt"], "kind": "digest", "apply": "auto"},
         "Document not found"),
        ("list_pipelines", {"document_id": digest_b["theme"]}, "Document not found"),
    ]
    for name, body, detail in cases:
        resp = await _tool(client, name, key_a, **body)
        assert resp.status_code == 404, (name, resp.status_code, resp.text)
        assert resp.json()["detail"] == detail, (name, resp.text)
    live = await test_db.query(
        "SELECT * FROM agent_configs WHERE project_id = $p AND deleted_at IS NONE", {"p": pid_b},
    )
    assert {r["config_doc_id"] for r in live} == {digest_b["cfg"], extractor_b["cfg"]}
    listed = (await _tool(client, "list_pipelines", key_a)).json()
    assert [r["config_id"] for r in listed] == []  # project A has no bindings; B never leaks

    # A subtree key of B: the subtree wall is the distinct scope 403.
    sub_root = await _doc(client, admin_token, pid_b, "Subtree root")
    sub_cfg = await _doc(client, admin_token, pid_b, "Subtree config", parent_id=sub_root, content=CONFIG)
    outside = await _doc(client, admin_token, pid_b, "Outside the subtree")
    key_sub = await _key(admin_uid, pid_b, scope_root=sub_root)
    resp = await _tool(client, "bind_pipeline", key_sub, document_id=sub_root, config_doc_id=sub_cfg,
                       target_doc_id=outside, apply="auto")
    assert resp.status_code == 403, resp.text
    assert f"root {sub_root}" in resp.json()["detail"]
    assert await test_db.query(
        "SELECT * FROM agent_configs WHERE document_id = $d", {"d": sub_root},
    ) == []

    # A full member who did not create the digest cannot launch it (403, as REST).
    resp = await _tool(client, "run_pipeline", await _key(user_uid, pid_b),
                       config_id=digest_b["id"], apply="auto")
    assert resp.status_code == 403, resp.text

    # Unconfirmed (no verdict, apply defaults to confirm): 409 and NO binding.
    key_b = await _key(admin_uid, pid_b)
    theme_u = await _doc(client, admin_token, pid_b, "Unconfirmed theme")
    cfg_u = await _doc(client, admin_token, pid_b, "Unconfirmed config", content=CONFIG)
    tgt_u = await _doc(client, admin_token, pid_b, "Unconfirmed reports")
    resp = await _tool(client, "bind_pipeline", key_b, document_id=theme_u, kind="digest",
                       config_doc_id=cfg_u, target_doc_id=tgt_u)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "confirmation_required"
    assert await test_db.query(
        "SELECT * FROM agent_configs WHERE document_id = $d", {"d": theme_u},
    ) == []

    # The admin's tombstone closes every tool before any other answer.
    resp = await client.put("/api/admin/skills/pipelines", json={"enabled": False},
                            cookies={"lore_session": admin_token})
    assert resp.status_code == 200, resp.text
    try:
        for name, body in (
            ("list_pipelines", {}), ("pipeline_runs", {}),
            ("bind_pipeline", {"document_id": theme_u, "kind": "digest",
                               "config_doc_id": cfg_u, "target_doc_id": tgt_u, "apply": "auto"}),
            ("unbind_pipeline", {"config_id": digest_b["id"], "apply": "auto"}),
            ("run_pipeline", {"config_id": digest_b["id"], "apply": "auto"}),
        ):
            resp = await _tool(client, name, key_b, **body)
            assert resp.status_code == 403, (name, resp.text)
            assert resp.json()["detail"] == "pipelines is disabled by the administrator", name
        assert await test_db.query(
            "SELECT * FROM agent_configs WHERE document_id = $d", {"d": theme_u},
        ) == []
    finally:
        await client.delete("/api/admin/skills/pipelines", cookies={"lore_session": admin_token})


# TEST-WHY: cross-process-contract — the run row is written by the web process at enqueue, flipped by the worker task, read back by the tool; the job_id collapse is real Redis's answer, so no enqueue fake is installed.
async def test_pipeline_run_log_lifecycle(client, test_db, collab_project, embed):
    from pipeline.core import runs as run_log

    from jobs.tasks.digest import digest_task

    pid, _d, admin_token, _ut, admin_uid, _uu = collab_project
    theme = await _doc(client, admin_token, pid, "Health")
    cfg = await _doc(client, admin_token, pid, "Health config", content=CONFIG)
    tgt = await _doc(client, admin_token, pid, "Health reports")
    hdr = await _key(admin_uid, pid)

    # Bind + schedule under one consent; list names the documents by title.
    resp = await _tool(client, "bind_pipeline", hdr, document_id=theme, kind="digest",
                       config_doc_id=cfg, target_doc_id=tgt, schedule={"interval_s": 3600},
                       apply="auto")
    assert resp.status_code == 200, resp.text
    bound = resp.json()
    cid, schedule_id = bound["config_id"], bound["schedule"]["schedule_id"]
    assert (bound["schedule"]["enabled"], bound["schedule"]["interval_s"]) == (True, 3600)
    [row] = (await _tool(client, "list_pipelines", hdr)).json()
    assert row["config_id"] == cid
    assert (row["document_title"], row["config_doc_title"], row["target_doc_title"]) == (
        "Health", "Health config", "Health reports",
    )
    assert row["schedule"]["schedule_id"] == schedule_id and row["last_run"] is None

    # Run: accepted; the second post collapses on the pending job_id — and is
    # not a second run (ONE queued row, the pending id reported back).
    first = (await _tool(client, "run_pipeline", hdr, config_id=cid, apply="auto")).json()
    assert first["status"] == "accepted" and first["run_id"]
    again = (await _tool(client, "run_pipeline", hdr, config_id=cid, apply="auto")).json()
    assert again == {"status": "already_pending", "run_id": first["run_id"]}
    runs = (await _tool(client, "pipeline_runs", hdr, config_id=cid)).json()
    assert [(r["run_id"], r["trigger"], r["status"]) for r in runs] == [(first["run_id"], "manual", "queued")]
    assert (await _tool(client, "list_pipelines", hdr)).json()[0]["last_run"]["run_id"] == first["run_id"]

    # The worker: an empty window is a deliberate no-output run.
    assert await digest_task({}, cid, pid, run_id=first["run_id"]) == {"status": "empty"}
    [run] = (await _tool(client, "pipeline_runs", hdr, config_id=cid)).json()
    assert run["status"] == "skipped" and run["started_at"] and run["finished_at"]

    # A broken config: `error` + the message on the row AND the error note on
    # the theme doc — the old trace stays beside the new one.
    resp = await client.patch(f"/api/documents/{cfg}", json={"content": "not a config"},
                              cookies={"lore_session": admin_token})
    assert resp.status_code == 200, resp.text
    rid = run_log.new_run_id()
    await run_log.record_queued(run_id=rid, project_id=pid, kind="digest", trigger="manual", config_id=cid)
    with pytest.raises(Exception):
        await digest_task({}, cid, pid, run_id=rid)
    [failed, *rest] = (await _tool(client, "pipeline_runs", hdr, config_id=cid)).json()
    assert (failed["run_id"], failed["status"]) == (rid, "error") and failed["error"]
    assert [r["run_id"] for r in rest] == [first["run_id"]]  # newest first
    notes = await test_db.query(
        "SELECT * FROM chat_sessions WHERE document_id = $d AND is_note = true", {"d": theme},
    )
    assert len(notes) == 1

    # A worker faster than the web process: the job reaches its terminal mark
    # inside the post itself — the row ends terminal, never stuck `queued`
    # (the marks are UPDATEs; a row written after the post would miss them).
    async def instant_worker(run_id: str) -> object:
        await run_log.mark_running(run_id)
        await run_log.mark_finished(run_id, "skipped")
        return object()

    async def collapsed_post(_run_id: str) -> None:
        return None

    launch_kw = {"project_id": pid, "kind": "digest", "trigger": "manual", "config_id": cid}
    fast = await run_log.launch(instant_worker, **launch_kw)
    assert await run_log.launch(collapsed_post, **launch_kw) is None
    runs = (await _tool(client, "pipeline_runs", hdr, config_id=cid)).json()
    assert [(r["run_id"], r["status"]) for r in runs] == [
        (fast, "skipped"), (rid, "error"), (first["run_id"], "skipped"),
    ]

    # Unbind: the schedule goes with the binding; the id stops being addressable.
    assert (await _tool(client, "unbind_pipeline", hdr, config_id=cid, apply="auto")).status_code == 200
    assert (await _tool(client, "list_pipelines", hdr)).json() == []
    assert await fetch_one("pipeline_schedules", schedule_id) is None
    resp = await _tool(client, "run_pipeline", hdr, config_id=cid, apply="auto")
    assert resp.status_code == 404, resp.text
