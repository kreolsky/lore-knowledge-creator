"""Real-DB integration tests for the agent tool surface (search/read).

# SYSTEM: chat-agent-tools-integration-tests — exercises the agent tools
# against a real SurrealDB with the schema applied. The mock-based suites
# (test_chat_agent_multitool.py and its siblings) cover logic branches cheaply but mock
# documents.service / the DB, so the schema boundary (for example, the non-nullable
# `documents.path` UNIQUE index) is never enforced. These tests guard exactly
# that boundary — the gap that let the `create_document` path-missing bug ship.

Readonly tools (search_materials, read_document) call the executors directly
against the real DB. The mutating-tool REST apply tests were deleted with the
proposal cluster (mid-turn approval replaced the /proposals apply endpoint).
"""



# ─── Helpers ────────────────────────────────────────────────────────────────


def _user(uid: str) -> dict:
    return {"user_id": uid}


async def _create_doc(test_db, *, doc_id: str, project_id: str, title: str,
                      content: str = "", parent_id: str | None = None,
                      is_reference: bool = False, path: str | None = None) -> str:
    from db import create_record
    payload = {
        "project_id": project_id, "parent_id": parent_id, "title": title,
        "content": content, "path": path or f"{doc_id}.md",
        "is_index": False, "is_reference": is_reference,
    }
    await test_db.query("DELETE type::record('documents', $id)", {"id": doc_id})
    await create_record("documents", doc_id, payload)
    return doc_id


# ─── search_materials (real DB) ─────────────────────────────────────────────


async def test_search_returns_fresh_doc_as_direct_hit(client, test_db, admin_user, project_with_doc):
    """A freshly-created doc (embeddings lag) is found by the direct layer."""
    pid, _idx, admin_uid = project_with_doc
    await _create_doc(
        test_db, doc_id="search-target-1", project_id=pid,
        title="Dragon Lore", content="Ancient wyrm chronicles",
    )
    from agent.readonly_executors import _search_materials_exec
    result = await _search_materials_exec(
        project_id=pid, user=_user(admin_uid), query="dragon", k=5,
    )
    hits = {h["doc_id"]: h for h in result["hits"]}
    assert "search-target-1" in hits, result
    assert hits["search-target-1"]["source"] == "direct"


async def test_search_excludes_doc_without_access(client, test_db, admin_user,
                                                  regular_user, project_with_doc):
    """A user without project/per-doc access never sees a doc's snippet."""
    pid, _idx, admin_uid = project_with_doc
    _regular_uid = regular_user[0]
    await _create_doc(
        test_db, doc_id="search-private-1", project_id=pid,
        title="Secret Dragon", content="hidden chronicles",
    )
    from agent.readonly_executors import _search_materials_exec
    result = await _search_materials_exec(
        project_id=pid, user=_user(_regular_uid), query="dragon", k=5,
    )
    ids = {h["doc_id"] for h in result["hits"]}
    assert "search-private-1" not in ids


# ─── read_document (real DB) ────────────────────────────────────────────────


async def test_read_returns_full_content(client, test_db, admin_user, project_with_doc):
    pid, _idx, admin_uid = project_with_doc
    await _create_doc(
        test_db, doc_id="read-target-1", project_id=pid,
        title="Readable Doc", content="line one\nline two",
    )
    from agent.readonly_executors import _read_document_exec
    result = await _read_document_exec(
        doc_id="read-target-1", project_id=pid, user=_user(admin_uid),
    )
    assert result.get("content") == "line one\nline two"
    assert result.get("title") == "Readable Doc"
    assert result.get("is_reference") is False


async def test_read_cross_project_rejected(client, test_db, admin_user, project_with_doc):
    pid, _idx, admin_uid = project_with_doc
    # A doc in a different project.
    other_pid = "other-project-read"
    await test_db.query("DELETE type::record('projects', $id)", {"id": other_pid})
    from db import create_record
    await create_record("projects", other_pid, {
        "name": "Other", "status": "active", "project_context": "",
        "owner_id": admin_uid,
    })
    await _create_doc(
        test_db, doc_id="read-xproj-1", project_id=other_pid,
        title="XProj", content="x",
    )
    from agent.readonly_executors import _read_document_exec
    result = await _read_document_exec(
        doc_id="read-xproj-1", project_id=pid, user=_user(admin_uid),
    )
    assert "error" in result


async def test_read_no_access_rejected(client, test_db, admin_user, regular_user,
                                       project_with_doc):
    pid, _idx, _admin_uid = project_with_doc
    regular_uid = regular_user[0]
    await _create_doc(
        test_db, doc_id="read-noaccess-1", project_id=pid,
        title="NoAccess", content="x",
    )
    from agent.readonly_executors import _read_document_exec
    result = await _read_document_exec(
        doc_id="read-noaccess-1", project_id=pid, user=_user(regular_uid),
    )
    assert "error" in result


async def test_read_does_not_resolve_title_to_doc(client, test_db, admin_user, project_with_doc):
    """D8 (plan mcp-tool-surface-redesign): addressing is id-only — the name fallback
    is REMOVED (titles are not unique, so a name is not an address). Passing a doc
    TITLE now returns not-found instead of resolving."""
    pid, _idx, admin_uid = project_with_doc
    await _create_doc(
        test_db, doc_id="read-named-1", project_id=pid,
        title="Named Chronicle", content="named content",
    )
    from agent.readonly_executors import _read_document_exec
    result = await _read_document_exec(
        doc_id="Named Chronicle", project_id=pid, user=_user(admin_uid),
    )
    assert "error" in result  # id-only contract: a title is not an address


