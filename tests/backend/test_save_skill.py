"""save_skill — the agent turns a finished task into a project skill.

Plan skill-authoring-skill. The SERVER assembles the frontmatter and owns the
placement (Skills folder via ensure_agent_system_docs); the upsert key is the
frontmatter NAME, never the title. Everything here goes through the generated
Tool-API route with a project agent key, the wire the dsh driver speaks.
"""

import hashlib
import secrets

import pytest


async def _make_agent_key(
    test_db, user_id, project_id, *, auto_apply=False, document_id="",
    internal=False,
):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"ss-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": document_id,
        "token_hash": token_hash, "label": "agent", "capabilities": ["agent"],
        "auto_apply": auto_apply, "internal": internal,
    })
    return token


async def _head_children(test_db, pid: str, head_id: str) -> list[dict]:
    return await test_db.query(
        "SELECT meta::id(id) AS id, title, content FROM documents "
        "WHERE project_id = $pid AND parent_id = $hid AND deleted_at IS NONE",
        {"pid": pid, "hid": head_id},
    ) or []


@pytest.fixture
def workspace_files(monkeypatch):
    """A fake workspace: {rel_path: bytes}. The SFTP read seam resolves in
    sandbox.files (the pattern of test_sandbox_file_bridge.sftp_seams); a
    path absent from the dict raises what the real seam raises — asyncssh's
    SFTPNoSuchFile, which read_workspace_file maps to the 404 (a fixture
    raising the HTTPException itself would test nothing: observed as a live
    500 on the first drive)."""
    from asyncssh import SFTPNoSuchFile
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    store: dict[str, bytes] = {}

    async def _fake_read_checked(ws_path: str, max_bytes: int):
        for key, data in store.items():
            if ws_path.endswith("/" + key):
                return ws_path, data
        raise SFTPNoSuchFile(f"No such file: {ws_path}")

    monkeypatch.setattr(sandbox.files, "_sftp_read_checked", _fake_read_checked)
    return store


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


async def _skills_folder_id(test_db, pid: str) -> str:
    from agent_config import ensure_agent_system_docs

    return (await ensure_agent_system_docs(pid))["skills_folder"]


async def _folder_children(test_db, pid: str, folder_id: str) -> list[dict]:
    return await test_db.query(
        "SELECT meta::id(id) AS id, title, content FROM documents "
        "WHERE project_id = $pid AND parent_id = $fid AND deleted_at IS NONE",
        {"pid": pid, "fid": folder_id},
    ) or []


# ════════════════════════════════════════════════════════════════════════════
# Registry surface
# ════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════
# Handler — create
# ════════════════════════════════════════════════════════════════════════════


class TestSaveSkillCreate:


    @pytest.mark.asyncio
    async def test_served_payload_lists_head_with_spec_child(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """The wire the plugin reads: load_skills_subtree → build_skill_docs
        shows the head with the Spec in child_docs (the `## Material` line)."""
        from agent_config_load import load_skills_subtree
        from agent_skills import build_skill_docs

        pid, _, admin_uid = project_with_doc
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

        resp = await client.post("/api/tool/save_skill", json={
            "name": "served-wire",
            "description": "Use when … — «фраза», EN.",
            "body": "# Served wire",
            "tools": ["sandbox_bash"],
            "spec": "facts",
            "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert resp.status_code == 200, resp.text

        wire = build_skill_docs(await load_skills_subtree(pid))
        heads = [
            w for w in wire
            if w["content"].startswith("---\nname: served-wire\n")
        ]
        assert len(heads) == 1
        child_ids = {c["id"] for c in heads[0]["child_docs"]}
        assert resp.json()["spec_doc_id"] in child_ids


# ════════════════════════════════════════════════════════════════════════════
# Handler — upsert
# ════════════════════════════════════════════════════════════════════════════


class TestSaveSkillUpsert:
    @pytest.mark.asyncio
    async def test_same_name_updates_in_place(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """A second call with the same name is ONE head (no second catalog
        entry) and the SAME Spec doc id — the head's `## Material` line
        advertises that id, so churn would break what the model just read."""
        from helpers import skill_frontmatter

        pid, _, admin_uid = project_with_doc
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

        first = await client.post("/api/tool/save_skill", json={
            "name": "upsert-me",
            "description": "first",
            "body": "# First",
            "tools": ["sandbox_bash"],
            "spec": "v1 spec",
            "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert first.status_code == 200, first.text

        second = await client.post("/api/tool/save_skill", json={
            "name": "upsert-me",
            "description": "second",
            "body": "# Second",
            "tools": ["sandbox_bash"],
            "spec": "v2 spec",
            "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert second.status_code == 200, second.text
        assert second.json()["doc_id"] == first.json()["doc_id"]
        assert second.json()["spec_doc_id"] == first.json()["spec_doc_id"]

        folder_id = await _skills_folder_id(test_db, pid)
        children = await _folder_children(test_db, pid, folder_id)
        assert len(children) == 1
        meta = skill_frontmatter(children[0]["content"])
        assert meta["description"] == "second"
        assert meta["body"] == "# Second"
        spec_rows = await test_db.query(
            "SELECT content FROM documents WHERE project_id = $pid "
            "AND parent_id = $hid AND deleted_at IS NONE",
            {"pid": pid, "hid": first.json()["doc_id"]},
        )
        assert spec_rows[0]["content"] == "v2 spec"


# ════════════════════════════════════════════════════════════════════════════
# Refusals
# ════════════════════════════════════════════════════════════════════════════


class TestSaveSkillRefusals:


    @pytest.mark.asyncio
    async def test_non_member_principal_403s(
        self, client, test_db, regular_user, project_with_doc,
    ):
        pid, _, _ = project_with_doc
        regular_uid = regular_user[0]
        agent_tok = await _make_agent_key(test_db, regular_uid, pid)
        resp = await client.post("/api/tool/save_skill", json={
            "name": "no-access", "description": "x", "body": "y",
            "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_confirm_without_verdict_refuses_then_marker_applies(
        self, client, test_db, admin_user, project_with_doc, monkeypatch,
    ):
        """apply defaults to confirm: the machine-readable ask signal (409
        confirmation_required), then the attested driver retry applies — one
        consent covers head + Spec together (copied from
        test_rename_document.py's marker round-trip)."""
        import config

        monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "drv-s3cret")
        pid, _, admin_uid = project_with_doc
        agent_tok = await _make_agent_key(test_db, admin_uid, pid)

        payload = {
            "name": "held-skill",
            "description": "x",
            "body": "# Held",
            "tools": ["sandbox_bash"],
            "spec": "held spec",
            "apply": "confirm",
        }
        resp = await client.post(
            "/api/tool/save_skill", json=payload, headers=_hdr(agent_tok),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "confirmation_required"

        # An UNATTESTED marker changes nothing — the header alone is any
        # agent key's claim about itself.
        resp_bare = await client.post("/api/tool/save_skill", json=payload, headers={
            **_hdr(agent_tok), "X-Agent-Verdict": "allowed-once",
        })
        assert resp_bare.status_code == 409, resp_bare.text

        # The driver's retry shape: the same call, the marker, the secret.
        resp2 = await client.post("/api/tool/save_skill", json=payload, headers={
            **_hdr(agent_tok),
            "X-Agent-Verdict": "allowed-once",
            "X-Driver-Secret": "drv-s3cret",
        })
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["status"] == "applied"
        assert resp2.json()["spec_doc_id"]


# ════════════════════════════════════════════════════════════════════════════
# Handler — files (a skill's scripts live in the graph)
# ════════════════════════════════════════════════════════════════════════════


SCRIPT = "import sys\n\nprint('stars', sys.argv[1])\n\t# tab kept\n"


class TestSaveSkillFiles:
    async def _save(self, client, tok, name, files=None, **extra):
        payload = {
            "name": name, "description": "Use when … — «фраза», EN.",
            "body": "# Files", "apply": "auto", **extra,
        }
        if files is not None:
            payload["files"] = files
        return await client.post(
            "/api/tool/save_skill", json=payload, headers=_hdr(tok),
        )

    @pytest.mark.asyncio
    async def test_files_become_path_titled_children_verbatim(
        self, client, test_db, project_with_doc, workspace_files,
    ):
        """One child per file, title == path, content byte-identical to the
        workspace file (no fence, no frontmatter — the round trip is the
        whole contract)."""
        pid, _, uid = project_with_doc
        tok = await _make_agent_key(test_db, uid, pid, auto_apply=True, internal=True)
        workspace_files["work/run.py"] = SCRIPT.encode()
        workspace_files["notes/api.md"] = b"# API\n\nfield `stargazers_count`\n"

        resp = await self._save(client, tok, "with-files", files=[
            {"path": "scripts/run.py", "sandbox_path": "work/run.py"},
            {"path": "references/api.md", "sandbox_path": "notes/api.md"},
        ])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        children = await _head_children(test_db, pid, body["doc_id"])
        by_title = {c["title"]: c for c in children}
        assert set(by_title) == {"scripts/run.py", "references/api.md"}
        assert by_title["scripts/run.py"]["content"] == SCRIPT
        assert by_title["references/api.md"]["content"] == "# API\n\nfield `stargazers_count`\n"
        assert body["files"] == {
            "scripts/run.py": by_title["scripts/run.py"]["id"],
            "references/api.md": by_title["references/api.md"]["id"],
        }


    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["../x", "bin/x", "scripts/../x", "scripts/",
                                     "scripts/a/b/c", "/scripts/x"])
    async def test_path_outside_grammar_is_422(
        self, client, test_db, project_with_doc, workspace_files, bad,
    ):
        pid, _, uid = project_with_doc
        tok = await _make_agent_key(test_db, uid, pid, auto_apply=True, internal=True)
        workspace_files["run.py"] = b"x"
        resp = await self._save(client, tok, "bad-path", files=[
            {"path": bad, "sandbox_path": "run.py"},
        ])
        assert resp.status_code == 422, resp.text


    @pytest.mark.asyncio
    async def test_missing_workspace_file_is_404_and_nothing_lands(
        self, client, test_db, project_with_doc, workspace_files,
    ):
        """The read happens BEFORE any write: a missing file is a 404 naming
        the workspace path, and no head / child exists afterwards."""
        pid, _, uid = project_with_doc
        tok = await _make_agent_key(test_db, uid, pid, auto_apply=True, internal=True)
        resp = await self._save(client, tok, "no-such-file", files=[
            {"path": "scripts/run.py", "sandbox_path": "gone/run.py"},
        ])
        assert resp.status_code == 404, resp.text
        assert "gone/run.py" in resp.json()["detail"]
        folder_id = await _skills_folder_id(test_db, pid)
        assert [c for c in await _folder_children(test_db, pid, folder_id)
                if c["title"] == "no-such-file"] == []
