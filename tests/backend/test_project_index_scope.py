"""Plan "project-index-as-project-face" — the index doc's subtree IS the whole
project, minus the system tree and the help guide.

TEST-WHY: security (the plan's own list). The index-scope rule
(scope.resolve_index_scope / _index_scope_ids) is the sandbox wall for a key
issued on the project's index doc: it covers every top-level doc, refuses the
system and help subtrees BY ANCESTRY, never resolves to the whole-project
sentinel, and refuses cross-project docs. Issuance of any key on the index is
owner-only; the index is unpublishable in both share funnels; the index-rooted
export is the whole-project zip; an agent create under an index key lands at
project root (normalize_parent); chat recall under_document_id=index covers a
chat anchored at a top-level doc; the structure map of an index key is the
project, not the index row.
"""
import hashlib
import io
import secrets
import zipfile

import pytest

# ─── helpers (per-file convention, mirrors test_subtree_scoped_agent_keys) ───


async def _make_key(
    test_db, user_id: str, project_id: str, *, document_id: str = "",
    capabilities: list[str], auto_apply: bool = False,
    internal: bool = False,
) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    key_id = f"idx-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": document_id,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "index test",
        "capabilities": capabilities,
        "auto_apply": auto_apply,
        "internal": internal,
    })
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_doc(
    client, token: str, project_id: str, title: str, content: str = "",
    *, parent_id: str | None = None,
) -> str:
    payload: dict = {"project_id": project_id, "title": title, "content": content}
    if parent_id:
        payload["parent_id"] = parent_id
    resp = await client.post(
        "/api/documents", json=payload, cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _seed_row(test_db, doc_id: str, row: dict) -> str:
    """Raw documents row (system/help skeleton rows are not REST-creatable)."""
    from db import create_record

    await test_db.query("DELETE type::record('documents', $id)", {"id": doc_id})
    await create_record("documents", doc_id, row)
    return doc_id


async def _seed_project_face(test_db, client, admin_user, project_with_doc):
    """Index + top-level A (child A1) + the system and help subtrees.

    Returns (pid, idx, a, a1, sys_root, sys_child, help_root, help_child,
    admin_uid, admin_token)."""
    pid, idx_id, admin_uid = project_with_doc
    _, admin_token = admin_user

    a = await _make_doc(client, admin_token, pid, "FaceA", "face a body")
    a1 = await _make_doc(client, admin_token, pid, "FaceA1", "face a1 body",
                         parent_id=a)

    sys_root = f"sys-system_root-{pid}"
    sys_child = f"sys-rules-entry-{pid}"
    await _seed_row(test_db, sys_root, {
        "project_id": pid, "parent_id": None, "title": "System",
        "content": "", "path": f".lore/system/{sys_root}", "is_system": True,
    })
    await _seed_row(test_db, sys_child, {
        "project_id": pid, "parent_id": sys_root, "title": "SysChild",
        "content": "secret system body", "path": f".lore/system/{sys_child}",
    })

    help_root = f"sys-help-{pid}-index"
    help_child = f"sys-help-{pid}-pages"
    await _seed_row(test_db, help_root, {
        "project_id": pid, "parent_id": None, "title": "Lore Guide",
        "content": "secret help body", "path": f".lore/help/{help_root}",
    })
    await _seed_row(test_db, help_child, {
        "project_id": pid, "parent_id": help_root, "title": "HelpPage",
        "content": "secret help page", "path": f".lore/help/{help_child}",
    })
    return (pid, idx_id, a, a1, sys_root, sys_child, help_root, help_child,
            admin_uid, admin_token)


# ─── the wall ────────────────────────────────────────────────────────────────


# TEST-WHY: refused-principal — an index key reads top-level docs and is 403-refused on the system/help subtrees; unit layer pins enumeration + cross-project.
@pytest.mark.asyncio
async def test_index_key_reads_project_refuses_system_and_help(client, test_db, admin_user, project_with_doc):
    from scope import in_subtree, subtree_doc_ids

    (pid, idx, a, a1, sys_root, sys_child, help_root, help_child,
     admin_uid, admin_token) = await _seed_project_face(
        test_db, client, admin_user, project_with_doc)
    foreign = await _seed_row(test_db, "idx-face-foreign", {
        "project_id": "some-other-project", "parent_id": None,
        "title": "Foreign", "content": "", "path": "idx-face-foreign.md",
    })
    # A help page MOVED OUT of the guide is an ordinary doc (ancestry rule).
    moved = await _make_doc(client, admin_token, pid, "MovedHelp", "moved",
                            parent_id=a)

    # Enumeration: the whole project minus system/help, plus the index itself.
    assert set(await subtree_doc_ids(idx, pid)) == {idx, a, a1, moved}
    # Membership answers the same rule.
    assert await in_subtree(idx, a) is True
    assert await in_subtree(idx, idx) is True
    assert await in_subtree(idx, sys_root) is False
    assert await in_subtree(idx, sys_child) is False
    assert await in_subtree(idx, help_root) is False
    assert await in_subtree(idx, help_child) is False
    # Cross-project docs are never in scope (the scan is project-bounded).
    assert await in_subtree(idx, foreign) is False

    # Enforcement through the tool surface: the top-level doc reads, the
    # system and help docs are refused with the scope 403.
    key = await _make_key(test_db, admin_uid, pid, document_id=idx,
                          capabilities=["agent"])
    resp = await client.post(
        "/api/tool/read_document", json={"document_id": a}, headers=_hdr(key),
    )
    assert resp.status_code == 200, resp.text
    assert "face a body" in resp.json()["content"]
    for doc_id in (sys_root, sys_child, help_root, help_child):
        resp = await client.post(
            "/api/tool/read_document", json={"document_id": doc_id},
            headers=_hdr(key),
        )
        assert resp.status_code == 403, f"{doc_id} must be out of scope"


# ─── the gates: owner-only keys, unpublishable shares ────────────────────────


# TEST-WHY: refused-principal — any key on the index is owner-only (full non-owner 403) and the index is unpublishable (mint 400, pre-guard row resolves 404).
@pytest.mark.asyncio
async def test_index_gates_owner_only_keys_and_unpublishable_shares(client, test_db, admin_user, regular_user, project_with_doc):
    from db import create_record

    pid, idx, *_ = project_with_doc
    _, admin_token = admin_user
    user_uid, user_token = regular_user
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )

    # ANY key (widget or agent) on the index is owner-only.
    for capabilities in (["widget"], ["agent"]):
        resp = await client.post(
            "/api/api-keys",
            json={"document_id": idx, "label": "k", "capabilities": capabilities},
            cookies={"lore_session": user_token},
        )
        assert resp.status_code == 403, capabilities
    resp = await client.post(
        "/api/api-keys",
        json={"document_id": idx, "label": "k", "capabilities": ["widget"]},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["token"]

    # Mint on the index is refused.
    resp = await client.post(
        f"/api/projects/{pid}/documents/{idx}/shares",
        json={"scope": "subtree"}, cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 400, resp.text

    # A pre-guard share row (users shared the index trying to share the
    # project) is 404 at resolve — never an anonymous whole-project link.
    await create_record("document_shares", f"idx-share-{secrets.token_hex(4)}", {
        "project_id": pid, "document_id": idx, "scope": "subtree",
        "token": "legacy-" + secrets.token_hex(8),
        "created_by": admin_user[0], "allow_comments": False,
    })
    resp = await client.get(f"/api/public/documents/{idx}")
    assert resp.status_code == 404, resp.text


# ─── the face surfaces: export, create, recall, structure ────────────────────


# TEST-WHY: cross-process-contract — the index key's export/create/recall/structure surfaces ride the real HTTP→service→Surreal stack with no seams.
@pytest.mark.asyncio
async def test_index_face_export_create_recall_structure(client, test_db, admin_user, project_with_doc):
    from db import fetch_one

    (pid, idx, a, a1, sys_root, _sys_child, help_root, _help_child,
     admin_uid, admin_token) = await _seed_project_face(
        test_db, client, admin_user, project_with_doc)
    # Give the index a body so the archive root README.md is written.
    patched = await client.patch(
        f"/api/documents/{idx}", json={"content": "the project face"},
        cookies={"lore_session": admin_token},
    )
    assert patched.status_code == 200, patched.text

    key = await _make_key(test_db, admin_uid, pid, document_id=idx,
                          capabilities=["agent"], auto_apply=True,
                          internal=True)

    # Export: the whole project under `<Project name>/`, no system/help files.
    resp = await client.get(
        f"/api/documents/{idx}/export/subtree",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert set(zf.namelist()) == {
            "Test Project/README.md",
            "Test Project/FaceA/README.md",
            "Test Project/FaceA/FaceA1.md",
        }
        assert "the project face" in zf.read("Test Project/README.md").decode()

    # Create with no parent: resolve_scoped_parent defaults to the index,
    # normalize_parent stores None — a top-level doc, not an index child.
    resp = await client.post(
        "/api/tool/create_document",
        json={"title": "Landed", "content": "body", "apply": "auto"},
        headers=_hdr(key),
    )
    assert resp.status_code == 200, resp.text
    row = await fetch_one("documents", resp.json()["doc_id"])
    assert not row.get("parent_id")

    # Recall under the index: a chat anchored at a top-level doc is found;
    # one anchored at the system root is not.
    top_chat, sys_chat = "sTop", "sSys"
    for sid, did in ((top_chat, a), (sys_chat, sys_root)):
        await test_db.query(
            "CREATE type::record('chat_sessions', $id) CONTENT $f",
            {"id": sid, "f": {"user_id": admin_uid, "project_id": pid,
                              "title": f"title {sid}", "model": "m",
                              "document_id": did}},
        )
    resp = await client.post(
        "/api/tool/list_my_chats", json={"under_document_id": idx},
        headers=_hdr(key),
    )
    assert resp.status_code == 200, resp.text
    assert [r["chat_id"] for r in resp.json()["rows"]] == [top_chat]

    # Structure: an index key's map is the project (index + top-level rows),
    # never the system or help roots.
    resp = await client.post(
        "/api/tool/get_project_structure", json={}, headers=_hdr(key),
    )
    assert resp.status_code == 200, resp.text
    ids = {d["document_id"] for d in resp.json()["documents"]}
    assert a in ids
    assert idx in ids
    assert sys_root not in ids
    assert help_root not in ids
