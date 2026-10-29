"""Export half of the subtree Markdown zip tools — export_subtree_md.

# SYSTEM: subtree-md (see backend/subtree_md.py) — the EXPORT direction: a Lore
# subtree → a zip of Markdown files (GitHub/Obsidian-compatible layout:
# dir + README.md per parent, Title.md per leaf, _assets/ for inline image
# binaries). The tool stages the zip and hands back a short-lived signed URL
# (never bytes as tool output).

Drives the real MCP surface (tools/call) with a read-write key, then downloads
the staged zip through the UNAUTHENTICATED serve route — the same route an
external agent uses. The REST half (GET /api/documents/{id}/export/subtree,
the browser zip for the snapshot tab) rides the same domain exporter and is
covered at the bottom.
"""
import hashlib
import io
import json
import secrets
import uuid
import zipfile

import pytest
from helpers import make_token

# ─── fixtures: project + agent keys ───────────────────────────────────────────


class _Project:
    def __init__(self, pid: str):
        self.pid = pid


@pytest.fixture
async def make_project(test_db, admin_user):
    """A fresh project owned by the admin (unique pid — xdist-safe)."""
    from db import create_record

    uid, _tok = admin_user
    pid = "subtree-md-" + uuid.uuid4().hex[:10]
    await create_record("projects", pid, {
        "name": "Subtree MD", "status": "active", "project_context": "",
        "owner_id": uid,
    })
    await create_record("project_members", "pm-" + pid, {
        "project_id": pid, "user_id": uid, "access_level": "full",
    })
    yield _Project(pid)
    await test_db.query("DELETE documents WHERE project_id = $pid", {"pid": pid})
    await test_db.query("DELETE project_members WHERE project_id = $pid", {"pid": pid})
    await test_db.query("DELETE type::record('projects', $pid)", {"pid": pid})


async def _make_agent_key(test_db, user_id: str, project_id: str, *,
                          auto_apply: bool, scope_root: str = "") -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    key_id = f"subtree-md-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id,
        "document_id": scope_root, "token_hash": hashlib.sha256(
            token.encode()).hexdigest(),
        "label": "subtree-md test", "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


@pytest.fixture
async def rw_key(test_db, make_project, admin_user):
    return await _make_agent_key(
        test_db, admin_user[0], make_project.pid, auto_apply=True,
    )


@pytest.fixture
def scoped_key(test_db, make_project, admin_user):
    """Factory: an agent key scoped to a subtree root; returns (token, root)."""

    async def _make(scope_root: str) -> tuple[str, str]:
        token = await _make_agent_key(
            test_db, admin_user[0], make_project.pid,
            auto_apply=True, scope_root=scope_root,
        )
        return token, scope_root

    return _make


# ─── seeding helpers ──────────────────────────────────────────────────────────


async def _seed_doc(db, pid: str, doc_id: str, title: str, content: str,
                    parent: str | None) -> str:
    from db import create_record

    await create_record("documents", doc_id, {
        "project_id": pid, "parent_id": parent, "title": title,
        "content": content, "path": f"{doc_id}.md", "is_index": False,
    })
    return doc_id


PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


async def _seed_image_ref(db, pid: str, ref_id: str, host: str) -> str:
    """A real image reference row + file on disk (the export copies the bytes)."""
    from config import STORAGE_PATH
    from db import create_record

    rel = f"{pid}/{ref_id}/pic.png"
    path = STORAGE_PATH / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_BYTES)
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": host, "title": "pic.png",
        "content": "", "path": f"_ref/{ref_id}.md", "is_reference": True,
        "media_type": "image", "file_path": rel,
        "file_meta": {"mime_type": "image/png", "original_name": "pic.png",
                      "file_size": len(PNG_BYTES)},
    })
    return ref_id


async def _export(client, token: str, root_id: str) -> tuple[dict, bytes]:
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "export_subtree_md",
                         "arguments": {"root_id": root_id}}},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert not body["result"].get("isError"), body["result"]
    result = json.loads(body["result"]["content"][0]["text"])
    dl = await client.get(result["url"])
    assert dl.status_code == 200, dl.text
    return result, dl.content


def _zip_names(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return sorted(zf.namelist())


def _read(data: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(name).decode("utf-8")


# ─── layout: INDEX rules, empty-leaf skip, dedup, reserved names ──────────────


@pytest.mark.asyncio
async def test_export_sibling_title_dedup(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    root = await _seed_doc(test_db, pid, "x-root", "R", "root body", None)
    await _seed_doc(test_db, pid, "x-d1", "Same", "one", root)
    await _seed_doc(test_db, pid, "x-d2", "Same", "two", root)
    _result, data = await _export(client, rw_key, root)
    assert _zip_names(data) == ["R/README.md", "R/Same (2).md", "R/Same.md"]
    assert _read(data, "R/Same.md") == "one"
    assert _read(data, "R/Same (2).md") == "two"


@pytest.mark.asyncio
async def test_export_reserved_entry_names_escaped(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    root = await _seed_doc(test_db, pid, "x-root", "R", "root body", None)
    await _seed_doc(test_db, pid, "x-r1", "README", "content", root)
    await _seed_doc(test_db, pid, "x-r2", "_assets", "content2", root)
    _result, data = await _export(client, rw_key, root)
    names = _zip_names(data)
    # `R/README.md` is the ROOT's own content file — the mechanism itself. What
    # must hold: no CHILD exported onto a reserved entry name (a child named
    # README must NOT become the parent's content on re-import).
    assert names == ["R/README (doc).md", "R/README.md", "R/_assets (doc).md"]
    assert _read(data, "R/README.md") == "root body"
    assert _read(data, "R/README (doc).md") == "content"
    assert _read(data, "R/_assets (doc).md") == "content2"


# ─── link + embed translation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_links_inside_and_outside_subtree(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    root = await _seed_doc(test_db, pid, "x-root", "R", "root body", None)
    leaf = await _seed_doc(test_db, pid, "x-leaf", "Leaf", "leaf body", root)
    parent = await _seed_doc(test_db, pid, "x-parent", "Parent Doc", "p", root)
    await _seed_doc(test_db, pid, "x-sub", "Sub", "s", parent)
    outside = await _seed_doc(test_db, pid, "x-out", "Outside", "out body", None)

    await _seed_doc(
        test_db, pid, "x-src", "Src",
        f"- [to leaf]({leaf})\n"
        f"- [to parent]({parent})\n"
        f"- [outside]({outside})\n"
        f"- [ext](https://example.com/a)\n",
        root,
    )
    _result, data = await _export(client, rw_key, root)
    text = _read(data, "R/Src.md")
    assert "[to leaf](./Leaf.md)" in text
    assert "[to parent](./Parent%20Doc/README.md)" in text
    # A target outside the exported subtree becomes plain text, no link.
    assert "outside" in text and "[outside](" not in text
    # External URLs are left verbatim.
    assert "[ext](https://example.com/a)" in text


@pytest.mark.asyncio
async def test_export_embed_split_inside_vs_outside(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    root = await _seed_doc(test_db, pid, "x-root", "R", "root body", None)
    inner = await _seed_doc(test_db, pid, "x-inner", "Inner", "INNER BODY", root)
    outside = await _seed_doc(
        test_db, pid, "x-out", "Outside", "OUTSIDE BODY", None,
    )
    await _seed_doc(
        test_db, pid, "x-src", "Src",
        f"![in]({inner})\n\n![out]({outside})\n",
        root,
    )
    _result, data = await _export(client, rw_key, root)
    text = _read(data, "R/Src.md")
    # Target inside → Obsidian live embed as a relative link.
    assert "![in](./Inner.md)" in text
    # Target outside → the target's text inlined (one level), not a link.
    assert "OUTSIDE BODY" in text
    assert "![out](" not in text


# ─── tables + images ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_image_ref_becomes_assets_binary(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    root = await _seed_doc(test_db, pid, "x-root", "R", "root body", None)
    leaf = await _seed_doc(test_db, pid, "x-leaf", "Leaf", "", root)
    ref = await _seed_image_ref(test_db, pid, "x-img-ref", leaf)
    await _seed_doc(
        test_db, pid, "x-src", "Src", f"![alt|10x20](ref:{ref})\n", root,
    )
    # A LEAF's images live in its PARENT dir's _assets (a file cannot own a dir).
    await _seed_doc(
        test_db, pid, "x-leaf2", "Leaf2", f"![pic](ref:{ref})\n", root,
    )
    _result, data = await _export(client, rw_key, root)
    names = _zip_names(data)
    assert "R/_assets/pic.png" in names, names
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.read("R/_assets/pic.png") == PNG_BYTES
    text = _read(data, "R/Src.md")
    # Relative link, dimension suffix stripped from the alt.
    assert "![alt](_assets/pic.png)" in text
    assert _read(data, "R/Leaf2.md") == "![pic](_assets/pic.png)\n"


# ─── surface: staged URL lifecycle, keys, scope, errors ───────────────────────


@pytest.mark.asyncio
async def test_export_returns_short_lived_url_and_counts(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    root = await _seed_doc(test_db, pid, "x-root", "R", "body", None)
    await _seed_doc(test_db, pid, "x-child", "C", "c", root)
    result, _data = await _export(client, rw_key, root)
    assert result["status"] == "ok"
    assert result["root_id"] == root
    assert result["documents"] == 2
    assert result["files"] == 2
    assert result["url"].startswith("http://test/api/mcp/staged/")
    assert result["expires_at"]
    # The serve route is unauthenticated and token-bound: a forged token 403s.
    bad = await client.get("/api/mcp/staged/not-a-token")
    assert bad.status_code == 403


@pytest.mark.asyncio
async def test_export_out_of_scope_root_is_403(
    client, mcp_running, test_db, make_project, scoped_key,
):
    pid = make_project.pid
    wall = await _seed_doc(test_db, pid, "x-wall", "Wall", "w", None)
    inside = await _seed_doc(test_db, pid, "x-inside", "In", "i", wall)
    outside = await _seed_doc(test_db, pid, "x-outside", "Out", "o", None)
    token, scope_root = await scoped_key(wall)

    # Inside the scope root: fine.
    _result, _data = await _export(client, token, inside)
    # The scope root itself: fine (root is its own subtree).
    _result, _data = await _export(client, token, wall)
    # Outside: a distinct 403 naming the wall.
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "export_subtree_md",
                         "arguments": {"root_id": outside}}},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    body = resp.json()
    assert body["result"].get("isError")
    assert "outside this agent key's subtree scope" in json.dumps(body)


# ─── REST surface: GET /api/documents/{id}/export/subtree (snapshot tab) ──────
#
# The browser twin of the tool: same domain exporter, different transport —
# the zip streams inline with Content-Disposition (no staging, no token),
# because a browser can receive bytes and an agent cannot.


async def _rest_user(test_db, uid: str, name: str) -> str:
    """A registered user row + bearer token (no login round-trip needed)."""
    from db import create_record

    await create_record("users", uid, {
        "name": name, "email": f"{name}@test.com", "role": "user",
        "user_facts": "",
    })
    return make_token(uid, name, "user", f"{name}@test.com")


def _session(token: str) -> dict:
    """REST auth is the lore_session cookie (Bearer is the MCP/API-key path)."""
    return {"cookies": {"lore_session": token}}


@pytest.mark.asyncio
async def test_rest_subtree_export_readonly_member_gets_zip(
    client, test_db, make_project,
):
    """Read access is enough — the zip is a read-only artifact."""
    from db import create_record

    pid = make_project.pid
    uid = "subtree-readonly-" + uuid.uuid4().hex[:8]
    token = await _rest_user(test_db, uid, "readonly-member")
    await create_record("project_members", f"pm-{uid}", {
        "project_id": pid, "user_id": uid, "access_level": "readonly",
    })
    root = await _seed_doc(test_db, pid, "r-root", "R", "body", None)
    await _seed_doc(test_db, pid, "r-child", "C", "c", root)

    resp = await client.get(
        f"/api/documents/{root}/export/subtree", **_session(token),
    )
    assert resp.status_code == 200, resp.text
    assert _zip_names(resp.content)


@pytest.mark.asyncio
async def test_rest_subtree_export_non_member_is_exactly_404(
    client, test_db, make_project,
):
    """A registered non-member gets exactly 404 — missing and forbidden are
    one answer (require_document_read posture)."""
    pid = make_project.pid
    token = await _rest_user(
        test_db, "subtree-stranger-" + uuid.uuid4().hex[:8], "stranger",
    )
    root = await _seed_doc(test_db, pid, "r-root", "R", "body", None)

    resp = await client.get(
        f"/api/documents/{root}/export/subtree", **_session(token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rest_subtree_export_deleted_root_is_404(
    client, test_db, make_project, admin_user,
):
    """Soft-deleted root: the row keeps project_id, so access may resolve —
    the gate (fetch_doc_meta or the route's own deleted check) must 404."""
    pid = make_project.pid
    root = await _seed_doc(test_db, pid, "r-root", "R", "body", None)
    await test_db.query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": root},
    )

    resp = await client.get(
        f"/api/documents/{root}/export/subtree", **_session(admin_user[1]),
    )
    assert resp.status_code == 404
