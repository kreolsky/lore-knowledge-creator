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


def test_save_skill_registry_shape():
    """Agent + Tool-API only, mutating: never advertised over MCP (the MCP
    surface must not author the agent's own config), always served (no env
    gate), behind the apply gate."""
    from agent_tools import registry

    entry = registry.by_name("save_skill")
    assert entry.mutating is True
    assert set(entry.surfaces) == {"agent", "tool_api"}
    assert entry.env_gate is None

    from agent.tools import AGENT_TOOLS

    names = {t["function"]["name"] for t in AGENT_TOOLS}
    assert "save_skill" not in names


async def test_save_skill_is_core_not_packed():
    """CORE = served minus every seeded pack (the derivation
    test_skill_deep_research uses). A skill that packed save_skill would take
    it away from a chat that skipped that skill's load — the exact reason the
    tool is core."""
    import agent_skills
    from agent.tools import agent_toolset
    from helpers import pinned_tool_gates, skill_frontmatter

    with pinned_tool_gates(sandbox=True):
        served = {t["function"]["name"] for t in await agent_toolset()}
    packed: set[str] = set()
    for path in sorted(agent_skills.CONFIGS_DIR.glob("skill_*.md")):
        packed.update(skill_frontmatter(path.read_text(encoding="utf-8"))["tools"])
    assert "save_skill" in served
    assert "save_skill" in served - packed


# ════════════════════════════════════════════════════════════════════════════
# Handler — create
# ════════════════════════════════════════════════════════════════════════════


class TestSaveSkillCreate:
    @pytest.mark.asyncio
    async def test_creates_head_under_skills_folder(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """The head lands under the Skills folder with SERVER-assembled
        frontmatter that parses (helpers.skill_frontmatter) to the given
        name/description/tools — never hand-written YAML the plugin skips."""
        from helpers import skill_frontmatter

        pid, _, admin_uid = project_with_doc
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

        resp = await client.post("/api/tool/save_skill", json={
            "name": "repo-stats",
            "description": (
                "Use when the user asks for a repo's numbers — «сколько звёзд "
                "у репозитория», repo stars. Activates the console."
            ),
            "body": "# Repo stats\n\nFetch and report.",
            "tools": ["sandbox_bash"],
            "spec": "GET https://api.github.com/repos/{owner}/{repo} → "
                    "`stargazers_count`.",
            "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "applied"
        assert body["name"] == "repo-stats"

        folder_id = await _skills_folder_id(test_db, pid)
        children = await _folder_children(test_db, pid, folder_id)
        heads = [c for c in children if c["title"] == "repo-stats"]
        assert len(heads) == 1
        assert heads[0]["id"] == body["doc_id"]
        assert heads[0]["content"].startswith("---\nname: repo-stats\n")
        meta = skill_frontmatter(heads[0]["content"])
        assert meta["name"] == "repo-stats"
        assert meta["tools"] == ["sandbox_bash"]
        assert "сколько звёзд" in meta["description"]
        assert meta["body"] == "# Repo stats\n\nFetch and report."

        # The Spec child: one, titled Spec, under the head, with that content.
        spec_rows = await test_db.query(
            "SELECT meta::id(id) AS id, title, content FROM documents "
            "WHERE project_id = $pid AND parent_id = $hid "
            "AND deleted_at IS NONE",
            {"pid": pid, "hid": body["doc_id"]},
        )
        assert spec_rows and len(spec_rows) == 1
        assert spec_rows[0]["title"] == "Spec"
        assert spec_rows[0]["id"] == body["spec_doc_id"]
        assert "stargazers_count" in spec_rows[0]["content"]

    @pytest.mark.asyncio
    async def test_spec_omitted_creates_no_child(
        self, client, test_db, admin_user, project_with_doc,
    ):
        pid, _, admin_uid = project_with_doc
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

        resp = await client.post("/api/tool/save_skill", json={
            "name": "plain-procedure",
            "description": "Use when the user … — «фраза», EN phrase.",
            "body": "# Plain\n\nNo spec here.",
            "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert resp.status_code == 200, resp.text
        assert resp.json()["spec_doc_id"] is None

        rows = await test_db.query(
            "SELECT title FROM documents WHERE project_id = $pid "
            "AND parent_id = $hid AND deleted_at IS NONE",
            {"pid": pid, "hid": resp.json()["doc_id"]},
        )
        assert rows == []

        # And the assembled frontmatter carries an EMPTY pack (a prose-only
        # skill is always advertised — tools: [] must be explicit).
        folder_id = await _skills_folder_id(test_db, pid)
        children = await _folder_children(test_db, pid, folder_id)
        assert "tools: []" in children[0]["content"]

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

    @pytest.mark.asyncio
    async def test_renamed_title_head_is_still_matched(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """The upsert key is the frontmatter name, never the title — a user
        may rename the head in the tree."""
        pid, _, admin_uid = project_with_doc
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

        first = await client.post("/api/tool/save_skill", json={
            "name": "rename-proof",
            "description": "first",
            "body": "# First",
            "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert first.status_code == 200, first.text
        doc_id = first.json()["doc_id"]
        await test_db.query(
            "UPDATE type::record('documents', $id) SET title = 'Custom Title'",
            {"id": doc_id},
        )

        second = await client.post("/api/tool/save_skill", json={
            "name": "rename-proof",
            "description": "second",
            "body": "# Second",
            "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert second.status_code == 200, second.text
        assert second.json()["doc_id"] == doc_id
        folder_id = await _skills_folder_id(test_db, pid)
        children = await _folder_children(test_db, pid, folder_id)
        assert len(children) == 1

    @pytest.mark.asyncio
    async def test_upsert_without_spec_keeps_spec_byte_identical(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """«улучши скилл» loads the head, not the Spec — a description tweak
        must not wipe it. Omitted `spec` leaves the row untouched."""
        pid, _, admin_uid = project_with_doc
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

        first = await client.post("/api/tool/save_skill", json={
            "name": "keep-spec",
            "description": "first",
            "body": "# First",
            "spec": "precious facts\nline two",
            "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert first.status_code == 200, first.text

        second = await client.post("/api/tool/save_skill", json={
            "name": "keep-spec",
            "description": "tweaked trigger",
            "body": "# First improved",
            "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert second.status_code == 200, second.text
        assert second.json()["spec_doc_id"] == first.json()["spec_doc_id"]

        spec_rows = await test_db.query(
            "SELECT content FROM documents WHERE project_id = $pid "
            "AND parent_id = $hid AND deleted_at IS NONE",
            {"pid": pid, "hid": first.json()["doc_id"]},
        )
        assert spec_rows[0]["content"] == "precious facts\nline two"


# ════════════════════════════════════════════════════════════════════════════
# Refusals
# ════════════════════════════════════════════════════════════════════════════


class TestSaveSkillRefusals:
    @pytest.mark.asyncio
    async def test_bad_name_422s(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """The plugin's isSkillName grammar enforced server-side — a bad name
        is a 422 the model can self-correct from, never a silent catalog
        skip."""
        pid, _, admin_uid = project_with_doc
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)
        for bad in ("Bad_Name", "супер-скилл", "-leading", "trailing-", "a--b"):
            resp = await client.post("/api/tool/save_skill", json={
                "name": bad, "description": "x", "body": "y", "apply": "auto",
            }, headers=_hdr(agent_tok))
            assert resp.status_code == 422, (bad, resp.text)

    @pytest.mark.asyncio
    async def test_unknown_tool_name_422s_listing_served(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """The model writes ["bash"] more often than ["sandbox_bash"]; the
        detail must name every SERVED choice (derived from the registry's
        tool_api surface — never a literal)."""
        from agent_tools import registry

        pid, _, admin_uid = project_with_doc
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

        resp = await client.post("/api/tool/save_skill", json={
            "name": "bad-pack", "description": "x", "body": "y",
            "tools": ["bash"], "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert resp.status_code == 422, resp.text
        detail = str(resp.json()["detail"])
        served = {e.name for e in registry.tool_api_entries()}
        for name in served:
            assert name in detail, f"{name} missing from the 422 detail"

    @pytest.mark.asyncio
    async def test_shipped_skill_name_409s(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """A project copy shadows the shipped skill entirely — an operator
        act, refused here."""
        pid, _, admin_uid = project_with_doc
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)
        resp = await client.post("/api/tool/save_skill", json={
            "name": "deep-research", "description": "x", "body": "y",
            "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert resp.status_code == 409, resp.text

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
    async def test_files_omitted_on_upsert_keeps_children(
        self, client, test_db, project_with_doc, workspace_files,
    ):
        pid, _, uid = project_with_doc
        tok = await _make_agent_key(test_db, uid, pid, auto_apply=True, internal=True)
        workspace_files["run.py"] = SCRIPT.encode()
        first = await self._save(client, tok, "keep-files", files=[
            {"path": "scripts/run.py", "sandbox_path": "run.py"},
        ])
        assert first.status_code == 200, first.text
        before = await _head_children(test_db, pid, first.json()["doc_id"])

        second = await self._save(client, tok, "keep-files", body="# Tweaked")
        assert second.status_code == 200, second.text
        assert second.json()["files"] == {}
        after = await _head_children(test_db, pid, first.json()["doc_id"])
        assert [(c["id"], c["title"], c["content"]) for c in after] == \
            [(c["id"], c["title"], c["content"]) for c in before]

    @pytest.mark.asyncio
    async def test_files_given_on_upsert_replaces_content_same_id(
        self, client, test_db, project_with_doc, workspace_files,
    ):
        pid, _, uid = project_with_doc
        tok = await _make_agent_key(test_db, uid, pid, auto_apply=True, internal=True)
        workspace_files["run.py"] = b"v1\n"
        first = await self._save(client, tok, "bump-files", files=[
            {"path": "scripts/run.py", "sandbox_path": "run.py"},
        ])
        assert first.status_code == 200, first.text
        workspace_files["run.py"] = b"v2\n"
        second = await self._save(client, tok, "bump-files", files=[
            {"path": "scripts/run.py", "sandbox_path": "run.py"},
        ])
        assert second.status_code == 200, second.text
        assert second.json()["files"] == first.json()["files"]
        rows = await _head_children(test_db, pid, first.json()["doc_id"])
        assert [(r["title"], r["content"]) for r in rows] == [("scripts/run.py", "v2\n")]

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
    async def test_binary_file_is_422(
        self, client, test_db, project_with_doc, workspace_files,
    ):
        pid, _, uid = project_with_doc
        tok = await _make_agent_key(test_db, uid, pid, auto_apply=True, internal=True)
        workspace_files["blob.bin"] = b"\x89PNG\x00\x00binary"
        resp = await self._save(client, tok, "bin-file", files=[
            {"path": "references/blob.bin", "sandbox_path": "blob.bin"},
        ])
        assert resp.status_code == 422, resp.text
        assert "text" in resp.json()["detail"]
        folder_id = await _skills_folder_id(test_db, pid)
        assert [c for c in await _folder_children(test_db, pid, folder_id)
                if c["title"] == "bin-file"] == []

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

    @pytest.mark.asyncio
    async def test_files_need_the_internal_key(
        self, client, test_db, project_with_doc, workspace_files,
    ):
        """read_workspace_file carries the console-key gate: a non-internal
        key attaching files is refused, and nothing lands."""
        pid, _, uid = project_with_doc
        tok = await _make_agent_key(test_db, uid, pid, auto_apply=True, internal=False)
        workspace_files["run.py"] = b"x"
        resp = await self._save(client, tok, "ext-key", files=[
            {"path": "scripts/run.py", "sandbox_path": "run.py"},
        ])
        assert resp.status_code == 403, resp.text
        folder_id = await _skills_folder_id(test_db, pid)
        assert [c for c in await _folder_children(test_db, pid, folder_id)
                if c["title"] == "ext-key"] == []
