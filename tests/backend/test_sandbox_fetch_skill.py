"""sandbox_fetch_skill — a project skill's subtree materialized into the
caller's workspace.

Plan skill-files-projection. The read-only twin of sandbox_fetch_reference:
the destination is backend-derived (`skills/{project_id}/{name}/…`), the
SFTP write seam resolves in sandbox.files, and the three console gates are
copied from the fetch_reference tests in test_sandbox_file_bridge.py.
"""

import hashlib
import secrets

import pytest


async def _make_key(user_id, project_id, *, internal=True, document_id=""):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"fs-key-{secrets.token_hex(4)}", {
        "user_id": user_id, "project_id": project_id, "document_id": document_id,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent", "capabilities": ["agent"], "internal": internal,
        "auto_apply": True,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sftp_write(monkeypatch):
    """Sandbox configured + the SFTP write seam recorded as {rel_path: bytes}
    (rel to the caller's workspace)."""
    from helpers import pin_tool_gates
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    pin_tool_gates(monkeypatch, sandbox=True)
    writes: dict[str, bytes] = {}

    async def _fake_write(ws_path: str, data: bytes):
        writes[ws_path] = data

    monkeypatch.setattr(sandbox.files, "_sftp_write", _fake_write)
    return writes


async def _seed_skill(test_db, pid, uid, name, children: dict[str, str]) -> str:
    """A project skill head under the Skills folder + the given children
    (title → content), created through the same direct path save_skill uses."""
    from agent_config import ensure_agent_system_docs
    from api_key_auth import _resolve_user
    from routes.tool_api._common import _apply_create_document_direct

    folder_id = (await ensure_agent_system_docs(pid))["skills_folder"]
    user = await _resolve_user(uid)
    head = await _apply_create_document_direct(
        title=name, parent_id=folder_id, project_id=pid, user=user, scope_root="",
        content=f"---\nname: {name}\ndescription: \"x\"\ntools: []\n---\n\n# {name}\n",
    )
    for title, content in children.items():
        await _apply_create_document_direct(
            title=title, content=content, parent_id=head["doc_id"],
            project_id=pid, user=user, scope_root="",
        )
    return head["doc_id"]


def test_registry_shape():
    """Read-only, sandbox-gated, on exactly the surfaces sandbox_fetch_reference
    serves — derived from that entry, never a literal."""
    from agent_tools import registry

    entry = registry.by_name("sandbox_fetch_skill")
    twin = registry.by_name("sandbox_fetch_reference")
    assert entry.mutating is False
    assert entry.env_gate == "sandbox"
    assert set(entry.surfaces) == set(twin.surfaces)
    assert "mcp" not in entry.surfaces


@pytest.mark.asyncio
async def test_writes_skill_md_and_every_path_titled_child(
    client, test_db, project_with_doc, sftp_write,
):
    """SKILL.md + every `scripts/…` / `references/…` child land under
    `{ws}/skills/{project_id}/{name}`; Spec and a non-path child do NOT."""
    from routes.tool_api.sandbox.transport import workspace_for

    pid, _, uid = project_with_doc
    await _seed_skill(test_db, pid, uid, "repo-stats", {
        "Spec": "facts only",
        "scripts/run.py": "print('hi')\n",
        "references/api.md": "# api\n",
        "Notes": "not a file",
    })
    tok = await _make_key(uid, pid)
    resp = await client.post("/api/tool/sandbox_fetch_skill", json={
        "name": "repo-stats",
    }, headers=_hdr(tok))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    base = f"{workspace_for(uid)}/skills/{pid}/repo-stats"
    assert data["status"] == "applied"
    assert data["path"] == base
    assert sorted(data["files"]) == ["references/api.md", "scripts/run.py"]
    assert set(sftp_write) == {
        f"{base}/SKILL.md", f"{base}/scripts/run.py", f"{base}/references/api.md",
    }
    assert sftp_write[f"{base}/scripts/run.py"] == b"print('hi')\n"
    assert sftp_write[f"{base}/SKILL.md"].startswith(b"---\nname: repo-stats\n")


@pytest.mark.asyncio
async def test_unknown_name_is_404_listing_project_skills(
    client, test_db, project_with_doc, sftp_write,
):
    pid, _, uid = project_with_doc
    await _seed_skill(test_db, pid, uid, "known-one", {})
    tok = await _make_key(uid, pid)
    resp = await client.post("/api/tool/sandbox_fetch_skill", json={
        "name": "nope",
    }, headers=_hdr(tok))
    assert resp.status_code == 404, resp.text
    assert "known-one" in resp.json()["detail"]
    assert sftp_write == {}


@pytest.mark.asyncio
async def test_shipped_only_name_is_404(client, test_db, project_with_doc, sftp_write):
    """Shipped skills carry no files — a shipped name with no project copy
    resolves nothing (derived from the shipped layer, not a literal name)."""
    from agent_skills import frontmatter_name, shipped_skill_docs

    pid, _, uid = project_with_doc
    shipped = next(
        n for n in (frontmatter_name(d["content"]) for d in shipped_skill_docs()) if n
    )
    tok = await _make_key(uid, pid)
    resp = await client.post("/api/tool/sandbox_fetch_skill", json={
        "name": shipped,
    }, headers=_hdr(tok))
    assert resp.status_code == 404, resp.text
    assert sftp_write == {}


@pytest.mark.asyncio
async def test_external_key_is_403(client, test_db, project_with_doc, sftp_write):
    pid, _, uid = project_with_doc
    await _seed_skill(test_db, pid, uid, "s", {"scripts/run.py": "x"})
    tok = await _make_key(uid, pid, internal=False)
    resp = await client.post("/api/tool/sandbox_fetch_skill", json={"name": "s"},
                             headers=_hdr(tok))
    assert resp.status_code == 403, resp.text
    assert sftp_write == {}


@pytest.mark.asyncio
async def test_subtree_scoped_key_is_403(client, test_db, project_with_doc, sftp_write):
    pid, idx, uid = project_with_doc
    await _seed_skill(test_db, pid, uid, "s", {"scripts/run.py": "x"})
    tok = await _make_key(uid, pid, internal=True, document_id=idx)
    resp = await client.post("/api/tool/sandbox_fetch_skill", json={"name": "s"},
                             headers=_hdr(tok))
    assert resp.status_code == 403, resp.text
    assert sftp_write == {}


@pytest.mark.asyncio
async def test_commentator_is_403(client, test_db, project_with_doc, sftp_write):
    from db import create_record

    pid, _, uid = project_with_doc
    await _seed_skill(test_db, pid, uid, "s", {"scripts/run.py": "x"})
    guest = f"fs-guest-{secrets.token_hex(4)}"
    await create_record("users", guest, {
        "email": f"{guest}@sb.test", "name": guest, "role": "user",
        "password_hash": "x",
    })
    await create_record("project_members", f"fs-pm-{secrets.token_hex(4)}", {
        "project_id": pid, "user_id": guest, "access_level": "commentator",
    })
    tok = await _make_key(guest, pid, internal=True)
    resp = await client.post("/api/tool/sandbox_fetch_skill", json={"name": "s"},
                             headers=_hdr(tok))
    assert resp.status_code == 403, resp.text
    assert sftp_write == {}
