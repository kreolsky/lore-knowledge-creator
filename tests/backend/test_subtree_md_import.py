"""Import half of the subtree Markdown zip tools — import_subtree_md.

# SYSTEM: subtree-md (see backend/subtree_md.py) — the IMPORT direction: an
# md-folder zip (Obsidian-shaped) → a Lore subtree under a parent. Drives the
# real MCP surface for the staged-upload flow (mint → POST → staged_id →
# import) and the Tool-API surface for the sandbox_path flow, plus a full
# export→import round trip against test_subtree_md_export's exporter.
"""
import hashlib
import io
import json
import re
import secrets
import uuid
import zipfile
from unittest.mock import patch

import pytest

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


# ─── fixtures: project + agent keys (same shape as the export suite) ─────────


class _Project:
    def __init__(self, pid: str):
        self.pid = pid


@pytest.fixture
async def make_project(test_db, admin_user):
    from db import create_record

    uid, _tok = admin_user
    pid = "subtree-imp-" + uuid.uuid4().hex[:10]
    await create_record("projects", pid, {
        "name": "Subtree Import", "status": "active", "project_context": "",
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
                          auto_apply: bool, scope_root: str = "",
                          internal: bool = False) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    key_id = f"subtree-imp-key-{secrets.token_hex(4)}"
    row = {
        "user_id": user_id, "project_id": project_id,
        "document_id": scope_root, "token_hash": hashlib.sha256(
            token.encode()).hexdigest(),
        "label": "subtree-imp test", "capabilities": ["agent"],
        "auto_apply": auto_apply,
    }
    if internal:
        row["internal"] = True
    await create_record("api_keys", key_id, row)
    return token


@pytest.fixture
async def rw_key(test_db, make_project, admin_user):
    return await _make_agent_key(
        test_db, admin_user[0], make_project.pid, auto_apply=True,
    )


@pytest.fixture
async def ro_key(test_db, make_project, admin_user):
    return await _make_agent_key(
        test_db, admin_user[0], make_project.pid, auto_apply=False,
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


async def _seed_doc(db, pid: str, doc_id: str, title: str, content: str,
                    parent: str | None) -> str:
    from db import create_record

    await create_record("documents", doc_id, {
        "project_id": pid, "parent_id": parent, "title": title,
        "content": content, "path": f"{doc_id}.md", "is_index": False,
    })
    return doc_id


async def _seed_image_ref(db, pid: str, ref_id: str, host: str) -> str:
    """A real image reference row + file on disk (the export copies the
    bytes; the import then re-creates a reference from the zip binary)."""
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


# ─── MCP + zip helpers ────────────────────────────────────────────────────────


async def _mcp(client, token: str, name: str, arguments: dict) -> dict:
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _result(body: dict) -> dict:
    text = body["result"]["content"][0]["text"]
    return json.loads(text)


async def _stage_zip(client, token: str, parent_id: str | None,
                     files: dict[str, bytes | str]) -> str:
    """Mint the upload URL, POST the zip, return the staged_id."""
    body = await _mcp(client, token, "import_subtree_md",
                      {"parent_id": parent_id})
    mint = _result(body)
    assert mint["status"] == "upload_pending", mint
    assert mint["method"] == "POST"
    assert mint["field_name"] == "file"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    resp = await client.post(mint["url"], files={"file": ("subtree.zip", buf.getvalue())})
    assert resp.status_code == 200, resp.text
    staged = resp.json()
    assert staged["accepted"] is True
    assert re.match(r"^import-[0-9a-f]{32}$", staged["staged_id"])
    return staged["staged_id"]


async def _import_files(client, token: str, parent_id: str | None,
                        files: dict[str, bytes | str]) -> dict:
    staged_id = await _stage_zip(client, token, parent_id, files)
    body = await _mcp(client, token, "import_subtree_md",
                      {"parent_id": parent_id, "staged_id": staged_id})
    result = _result(body)
    assert result.get("status") == "applied", result
    return result


async def _tree(test_db, pid: str) -> list[dict]:
    rows = await test_db.query(
        "SELECT meta::id(id) AS id, parent_id, title, is_reference, media_type "
        "FROM documents WHERE project_id = $pid AND deleted_at IS NONE",
        {"pid": pid},
    )
    from db import extract_id

    out = []
    for r in rows or []:
        out.append({
            "id": r["id"],
            "parent": extract_id(r.get("parent_id")),
            "title": r.get("title"),
            "is_ref": bool(r.get("is_reference")),
            "media": r.get("media_type"),
        })
    return out


async def _content_of(test_db, doc_id: str) -> str:
    rows = await test_db.query(
        "SELECT content FROM type::record('documents', $id)", {"id": doc_id},
    )
    return (rows[0].get("content") if rows else "") or ""


# ─── layout: dirs, INDEX/README, empty dirs, .obsidian ────────────────────────


@pytest.mark.asyncio
async def test_import_dir_index_readme_and_empty_dir(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    result = await _import_files(client, rw_key, host, {
        "Guide/README.md": "readme body",
        "Guide/INDEX.md": "# index body",
        "Guide/Child.md": "child body",
        "Guide/Empty/anything.png": PNG_BYTES,  # dir with only assets → empty doc
        ".obsidian/workspace.json": "{}",
    })
    assert result["root_id"]
    tree = await _tree(test_db, pid)
    by_title = {d["title"]: d for d in tree if not d["is_ref"]}
    # INDEX.md wins over README.md as the parent's content.
    assert await _content_of(test_db, by_title["Guide"]["id"]) == "# index body"
    assert by_title["Guide"]["parent"] == host
    assert by_title["Child"]["parent"] == by_title["Guide"]["id"]
    # A dir holding only assets still becomes a document (the refs' host).
    assert "Empty" in by_title
    # .obsidian is not imported at all: no doc, no reference.
    assert not [d for d in tree if d["title"] == "workspace.json"]


@pytest.mark.asyncio
async def test_import_top_level_leaf_and_single_dir_root(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    result = await _import_files(client, rw_key, host, {
        "Solo.md": "solo body",
        "Pack/INDEX.md": "pack body",
    })
    # Two top-level entries → no single root doc.
    assert result["root_id"] is None
    assert result["documents"] == 2
    tree = await _tree(test_db, pid)
    by_title = {d["title"]: d for d in tree if not d["is_ref"]}
    assert by_title["Solo"]["parent"] == host
    assert by_title["Pack"]["parent"] == host
    assert await _content_of(test_db, by_title["Solo"]["id"]) == "solo body"


# ─── links: wiki, aliases, headings, relative md links ───────────────────────


@pytest.mark.asyncio
async def test_import_wiki_links_resolve_by_folder_then_unique(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    await _import_files(client, rw_key, host, {
        "B.md": "b root",
        "A.md": "see [[B]] and [[B|alias]] and [[B#heading]]",
        "Sub/B.md": "b sub",
        "Sub/C.md": "sub link [[B]]",
        "Sub3/X.md": "dup [[D]]",
        "D.md": "d root",
        "Sub2/D.md": "d sub2",
    })
    tree = await _tree(test_db, pid)
    docs = [d for d in tree if not d["is_ref"]]
    root_b = next(d for d in docs if d["title"] == "B" and d["parent"] == host)
    sub_doc = next(d for d in docs if d["title"] == "Sub")
    sub_b = next(d for d in docs if d["title"] == "B" and d["parent"] == sub_doc["id"])
    a = next(d for d in docs if d["title"] == "A")
    x = next(d for d in docs if d["title"] == "X")
    a_text = await _content_of(test_db, a["id"])
    # Root file: same-folder (root) match wins; alias kept; heading dropped
    # (deep targets are a documented non-goal).
    assert f"see [B]({root_b['id']})" in a_text
    assert f"[alias]({root_b['id']})" in a_text
    assert f"[B#heading]({root_b['id']})" in a_text
    # Sub/C.md: its own folder's B wins over the root B.
    c = next(d for d in docs if d["title"] == "C")
    c_text = await _content_of(test_db, c["id"])
    assert f"({sub_b['id']})" in c_text
    # Two D stems and neither in Sub3 → plain text, no link, no guess.
    x_text = await _content_of(test_db, x["id"])
    assert "dup D" in x_text
    assert "[[" not in x_text
    assert "](documents:" not in x_text


@pytest.mark.asyncio
async def test_import_relative_md_links_and_wiki_embed(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    await _import_files(client, rw_key, host, {
        "Note.md": "note body",
        "Folder/INDEX.md": "folder",
        "Folder/Kid.md": "kid",
        "L.md": (
            "one [n](./Note.md)\n"
            "two [f](Folder/INDEX.md)\n"
            "three [k](./Folder/Kid.md)\n"
            "four ![Note](Note.md)\n"
        ),
        "E.md": "embed ![[Note]]\n",
    })
    tree = await _tree(test_db, pid)
    by = {d["title"]: d for d in tree if not d["is_ref"]}
    note, kid = by["Note"]["id"], by["Kid"]["id"]
    l_text = await _content_of(test_db, by["L"]["id"])
    assert f"[n]({note})" in l_text
    assert f"[f]({by['Folder']['id']})" in l_text
    assert f"[k]({kid})" in l_text
    # A relative EMBED becomes our doc-embed syntax.
    assert f"![Note](doc:{note})" in l_text
    e_text = await _content_of(test_db, by["E"]["id"])
    assert f"![Note](doc:{note})" in e_text


@pytest.mark.asyncio
async def test_import_builds_mention_edges(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    await _import_files(client, rw_key, host, {
        "B.md": "b",
        "A.md": "go [[B]]",
    })
    tree = await _tree(test_db, pid)
    by = {d["title"]: d for d in tree if not d["is_ref"]}
    from db import extract_id

    edges = await test_db.query(
        "SELECT * FROM doc_mentions WHERE in = type::record('documents', $id)",
        {"id": by["A"]["id"]},
    )
    targets = {extract_id(e.get("out")) for e in (edges or [])}
    assert by["B"]["id"] in targets


# ─── tables + images ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_gfm_table_becomes_table_block(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    gfm = "| a | b |\n| --- | --- |\n| 1 | 2 |"
    await _import_files(client, rw_key, host, {"T.md": f"head\n\n{gfm}\n\ntail"})
    tree = await _tree(test_db, pid)
    doc = next(d for d in tree if d["title"] == "T")
    content = await _content_of(test_db, doc["id"])
    # The GFM block is replaced by a table anchor; the model lives in the ydoc.
    assert "| a | b |" not in content
    anchor = re.search(r"!\[Table\]\(table:([^)]+)\)", content)
    assert anchor, content
    from helpers import read_table_grid

    from ydoc_store import load


    grid = read_table_grid(await load(doc["id"]), anchor.group(1))
    assert grid["rows"] == [["a", "b"], ["1", "2"]]


@pytest.mark.asyncio
async def test_import_assets_become_references(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    await _import_files(client, rw_key, host, {
        "F/INDEX.md": "![alt](_assets/pic.png) and ![[pic.png]]",
        "F/_assets/pic.png": PNG_BYTES,
        "F/_assets/data.csv": "a,b\n1,2",
    })
    tree = await _tree(test_db, pid)
    f = next(d for d in tree if d["title"] == "F")
    # The assets bucket is NOT a document — no junk `_assets` doc may land
    # under F (the exporter's `F/_assets/` must re-import as refs only).
    assert "_assets" not in {
        d["title"] for d in tree if not d["is_ref"]
    }, tree
    refs = [d for d in tree if d["is_ref"] and d["parent"] == f["id"]]
    imgs = [d for d in refs if d["media"] == "image"]
    files = [d for d in refs if d["media"] == "file"]
    assert len(imgs) == 1 and len(files) == 1
    content = await _content_of(test_db, f["id"])
    # Both spellings rewrite to the SAME imported image ref.
    assert len(re.findall(r"!\[alt(\|\d+x\d+)?\]\(ref:", content)) == 1
    assert len(re.findall(r"!\[pic(\|\d+x\d+)?\]\(ref:", content)) == 1


# ─── round trip: export → import ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_round_trip_export_then_import(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    leaf = await _seed_doc(test_db, pid, "rt-leaf", "Leaf", "leaf body", None)
    gfm = "| h1 | h2 |\n| --- | --- |\n| x | y |"
    img = "rt-img"
    await _seed_doc(
        test_db, pid, "rt-src", "Src",
        f"[link]({leaf})\n\n![alt](ref:{img})\n\n{gfm}\n", None,
    )
    await _seed_image_ref(test_db, pid, img, "rt-src")
    root = await _seed_doc(test_db, pid, "rt-root", "Research", "root body", None)
    # Move Src + Leaf under the root (parent update via raw row for fixture
    # simplicity — the exporter only reads parent_id).
    for doc_id in (leaf, "rt-src"):
        await test_db.query(
            "UPDATE type::record('documents', $id) SET parent_id = $p",
            {"id": doc_id, "p": root},
        )
    # Export.
    resp = await _mcp(client, rw_key, "export_subtree_md", {"root_id": root})
    export = _result(resp)
    dl = await client.get(export["url"])
    assert dl.status_code == 200

    # Import the SAME zip under a fresh parent.
    host2 = await _seed_doc(test_db, pid, "rt-host2", "Host2", "", None)
    buf = io.BytesIO(dl.content)
    files = {
        name: zipfile.ZipFile(buf).read(name)
        for name in zipfile.ZipFile(buf).namelist()
    }
    result = await _import_files(client, rw_key, host2, files)
    assert result["root_id"], result

    tree = await _tree(test_db, pid)
    docs = [d for d in tree if not d["is_ref"]]
    new_root = next(d for d in docs if d["id"] == result["root_id"])
    assert new_root["title"] == "Research"
    assert new_root["parent"] == host2
    assert await _content_of(test_db, new_root["id"]) == "root body"
    kids = {d["title"]: d for d in docs if d["parent"] == new_root["id"]}
    assert set(kids) == {"Leaf", "Src"}
    src = kids["Src"]["id"]
    src_content = await _content_of(test_db, src)
    # The exported relative link re-imports as a mention edge; the GFM table
    # re-imports as a table block.
    assert f"[link]({kids['Leaf']['id']})" in src_content
    anchor = re.search(r"!\[Table\]\(table:([^)]+)\)", src_content)
    assert anchor, src_content
    from db import extract_id

    edges = await test_db.query(
        "SELECT * FROM doc_mentions WHERE in = type::record('documents', $id)",
        {"id": src},
    )
    assert kids["Leaf"]["id"] in {extract_id(e.get("out")) for e in (edges or [])}
    # The image binary round-trips: an image REF under the rebuilt Research
    # dir doc (the leaf's assets bucket) — and NO junk `_assets` document.
    assert "_assets" not in {d["title"] for d in docs}
    imgs = [d for d in tree
            if d["is_ref"] and d["media"] == "image"
            and d["parent"] == new_root["id"]]
    assert len(imgs) == 1
    assert re.search(r"!\[alt(\|\d+x\d+)?\]\(ref:", src_content)


# ─── surface: mutating gate, scope, sandbox_path, caps, zip-slip ──────────────


@pytest.mark.asyncio
async def test_import_readonly_key_is_403(
    client, mcp_running, test_db, make_project, ro_key,
):
    body = await _mcp(client, ro_key, "import_subtree_md", {"parent_id": None})
    assert body["result"].get("isError")
    assert "read-only" in json.dumps(body)


@pytest.mark.asyncio
async def test_import_scope_wall_and_default_parent(
    client, mcp_running, test_db, make_project, scoped_key,
):
    pid = make_project.pid
    wall = await _seed_doc(test_db, pid, "imp-wall", "Wall", "w", None)
    outside = await _seed_doc(test_db, pid, "imp-out", "Out", "o", None)
    token, _root = await scoped_key(wall)

    # An explicit parent outside the scope → the wall's distinct 403.
    body = await _mcp(client, token, "import_subtree_md", {"parent_id": outside})
    assert body["result"].get("isError")
    assert "subtree scope" in json.dumps(body)

    # An omitted parent resolves to the scope root — the import lands inside.
    await _import_files(client, token, None, {"N.md": "n"})
    tree = await _tree(test_db, pid)
    assert any(d["title"] == "N" and d["parent"] == wall for d in tree)


@pytest.mark.asyncio
async def test_import_via_sandbox_path(
    client, test_db, make_project, admin_user, monkeypatch,
):
    from helpers import pin_tool_gates
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import files as sandbox_files  # noqa: F401

    pin_tool_gates(monkeypatch, sandbox=True)
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    token = await _make_agent_key(
        test_db, admin_user[0], pid, auto_apply=True, internal=True,
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("W.md", "from workspace")
    monkeypatch.setattr(
        sandbox.files, "_sftp_read_checked",
        lambda ws_path, max_bytes: _async_const((ws_path, buf.getvalue())),
    )
    resp = await client.post(
        "/api/tool/import_subtree_md",
        json={"parent_id": host, "sandbox_path": "vault.zip"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"
    tree = await _tree(test_db, pid)
    assert any(d["title"] == "W" for d in tree)


async def _async_const(value):
    return value


@pytest.mark.asyncio
async def test_import_rejects_zip_slip(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    before = await _tree(test_db, pid)
    for evil in ("../evil.md", "/abs.md", "sub/../../up.md"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(evil, "payload")
        mint = _result(await _mcp(client, rw_key, "import_subtree_md",
                                  {"parent_id": host}))
        resp = await client.post(
            mint["url"], files={"file": ("subtree.zip", buf.getvalue())},
        )
        assert resp.status_code == 200, resp.text
        body = await _mcp(client, rw_key, "import_subtree_md",
                          {"parent_id": host, "staged_id": resp.json()["staged_id"]})
        assert body["result"].get("isError"), evil
    # A symlink entry is refused too.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link.png")
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "/etc/passwd")
    mint = _result(await _mcp(client, rw_key, "import_subtree_md",
                              {"parent_id": host}))
    resp = await client.post(
        mint["url"], files={"file": ("subtree.zip", buf.getvalue())},
    )
    body = await _mcp(client, rw_key, "import_subtree_md",
                      {"parent_id": host, "staged_id": resp.json()["staged_id"]})
    assert body["result"].get("isError")
    assert await _tree(test_db, pid) == before


@pytest.mark.asyncio
async def test_import_caps_enforced(
    client, mcp_running, test_db, make_project, rw_key,
):
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    # Whole-zip cap at REDEEM: a 1-byte zip against a 0 MB cap → 413.
    with patch("config.MAX_ARCHIVE_SIZE_MB", 0):
        mint = _result(await _mcp(client, rw_key, "import_subtree_md",
                                  {"parent_id": host}))
        resp = await client.post(
            mint["url"], files={"file": ("subtree.zip", b"PK\x03\x04 tiny")},
        )
        assert resp.status_code == 413, resp.text
    # Per-file markdown cap at import: a tiny md against a 0 MB cap → error.
    with patch("config.MAX_MARKDOWN_SIZE_MB", 0):
        staged = await _stage_zip(client, rw_key, host, {"A.md": "x"})
        body = await _mcp(client, rw_key, "import_subtree_md",
                          {"parent_id": host, "staged_id": staged})
        assert body["result"].get("isError")


def _zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_import_rejects_decompression_bomb(
    client, mcp_running, test_db, make_project, rw_key,
):
    """The declared-size fence in read_zip_tree: per-entry AND running-total,
    both BEFORE any entry is read (1MB archive cap patched → 10MB total;
    highly-compressible payloads stay far under the compressed-side cap)."""
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    before = await _tree(test_db, pid)

    per_entry = _zip_bytes([("bomb.md", b"0" * (1024 * 1024 + 1))])
    chunk = b"0" * (1024 * 1024 - 1024)  # just under the per-entry cap
    over_total = _zip_bytes([(f"n{i}.md", chunk) for i in range(11)])

    with patch("config.MAX_ARCHIVE_SIZE_MB", 1):
        for label, data in (("per-entry", per_entry), ("total", over_total)):
            mint = _result(await _mcp(client, rw_key, "import_subtree_md",
                                      {"parent_id": host}))
            resp = await client.post(
                mint["url"], files={"file": ("subtree.zip", data)},
            )
            assert resp.status_code == 200, (label, resp.text)
            body = await _mcp(
                client, rw_key, "import_subtree_md",
                {"parent_id": host, "staged_id": resp.json()["staged_id"]},
            )
            assert body["result"].get("isError"), label
            text = json.dumps(body)
            assert "too large decompressed" in text or "beyond the allowed" \
                in text, (label, text)
    # The fence fires before pass A — nothing was created.
    assert await _tree(test_db, pid) == before


# ─── atomicity + root assets bucket (review round 2) ──────────────────────────


@pytest.mark.asyncio
async def test_import_remote_image_rejected_before_any_create(
    client, mcp_running, test_db, make_project, rw_key,
):
    """Prevalidation: a remote image in ANY md file (here: the second one,
    after a perfectly good INDEX) refuses the whole import BEFORE pass A —
    no partial subtree."""
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    before = await _tree(test_db, pid)
    staged = await _stage_zip(client, rw_key, host, {
        "Guide/INDEX.md": "good body",
        "Guide/Bad.md": "text\n\n![leak](https://example.com/x.png)\n",
    })
    body = await _mcp(client, rw_key, "import_subtree_md",
                      {"parent_id": host, "staged_id": staged})
    assert body["result"].get("isError")
    assert "Remote image" in json.dumps(body)
    assert await _tree(test_db, pid) == before


@pytest.mark.asyncio
async def test_import_corrupt_assets_image_rejected_before_any_create(
    client, mcp_running, test_db, make_project, rw_key,
):
    """Prevalidation: an `_assets` binary the importer would refuse (magic
    mismatch) refuses the import up front too — the `_AssetImporter` gates
    can no longer fire mid-apply on bucket files."""
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    before = await _tree(test_db, pid)
    staged = await _stage_zip(client, rw_key, host, {
        "Guide/INDEX.md": "body",
        "Guide/_assets/fake.png": b"definitely not png bytes",
    })
    body = await _mcp(client, rw_key, "import_subtree_md",
                      {"parent_id": host, "staged_id": staged})
    assert body["result"].get("isError")
    assert "does not match declared type" in json.dumps(body)
    assert await _tree(test_db, pid) == before


@pytest.mark.asyncio
async def test_import_root_assets_bucket_hosts_on_parent(
    client, mcp_running, test_db, make_project, rw_key,
):
    """A ROOT-level `_assets/` bucket is not a document and not a silent
    drop: its files become references hosted on the IMPORT PARENT (the same
    host-for-dir rule pass B uses for root-level documents)."""
    pid = make_project.pid
    host = await _seed_doc(test_db, pid, "imp-host", "Host", "h", None)
    result = await _import_files(client, rw_key, host, {
        "_assets/pic.png": PNG_BYTES,
        "Note.md": "a lone note",
    })
    assert result["status"] == "applied"
    assert result["documents"] == 1  # Note.md only — no _assets doc
    assert result["references"] == 1
    tree = await _tree(test_db, pid)
    assert "_assets" not in {d["title"] for d in tree}
    imgs = [d for d in tree
            if d["is_ref"] and d["media"] == "image" and d["parent"] == host]
    assert len(imgs) == 1, tree
