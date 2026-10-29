"""Integration tests for checkpoint routes."""

import pytest


@pytest.mark.asyncio
async def test_list_checkpoints(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "ListCpDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    await client.post(
        "/api/checkpoints",
        json={"document_id": doc_id, "label": "cp1"},
        cookies={"lore_session": token},
    )
    resp = await client.get(
        f"/api/checkpoints?document_id={doc_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.asyncio
async def test_restore_emits_checkpoint_created_for_backup_row(
    client, admin_user, project_with_doc,
):
    """§3.4: restore_checkpoint must emit `checkpoint_created` for the before-restore
    _backup row so the history panel refreshes without a list reload."""
    from emit_recorder import EmitRecorder

    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": "EmitBackupDoc"}, cookies=cookies,
    )
    doc_id = resp.json()["document_id"]
    await client.patch(f"/api/documents/{doc_id}", json={"content": "V1 body"}, cookies=cookies)
    resp = await client.post(
        "/api/checkpoints", json={"document_id": doc_id, "label": "v1"}, cookies=cookies,
    )
    cp_id = resp.json()["checkpoint_id"]
    await client.patch(f"/api/documents/{doc_id}", json={"content": "V2 body"}, cookies=cookies)

    # Passthrough: the real bus still fans out so restore routing is unaffected.
    with EmitRecorder.active(passthrough=True) as rec:
        resp = await client.post(f"/api/checkpoints/{cp_id}/restore", cookies=cookies)
    assert resp.status_code == 200
    captured = rec.calls

    backup_emits = [
        c for c in captured
        if c[0] == "checkpoint_created"
        and c[1].get("entity_id") == doc_id
        and c[1]["event"]["checkpoint"].get("label") == "_backup"
    ]
    assert len(backup_emits) == 1, "restore must emit checkpoint_created for the _backup row"
    backup_cp = backup_emits[0][1]["event"]["checkpoint"]
    assert backup_cp.get("user_name") == "System"
    assert backup_cp.get("content") == "V2 body"


@pytest.mark.asyncio
async def test_delete_checkpoint(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "DelCpDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/checkpoints",
        json={"document_id": doc_id, "label": "todelete"},
        cookies={"lore_session": token},
    )
    cp_id = resp.json()["checkpoint_id"]
    resp = await client.delete(f"/api/checkpoints/{cp_id}", cookies={"lore_session": token})
    assert resp.status_code == 200


# ─── Restore integrity validation ───────────────────────────────────────────


async def _make_doc_with_checkpoint(client, token, pid, content="V1 content here"):
    """Create a document, set content, snapshot it. Returns (doc_id, cp_id)."""
    cookies = {"lore_session": token}
    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": "IntegrityDoc"}, cookies=cookies,
    )
    doc_id = resp.json()["document_id"]
    await client.patch(f"/api/documents/{doc_id}", json={"content": content}, cookies=cookies)
    resp = await client.post(
        "/api/checkpoints", json={"document_id": doc_id, "label": "v1"}, cookies=cookies,
    )
    return doc_id, resp.json()["checkpoint_id"]


@pytest.mark.asyncio
async def test_restore_with_live_session_uses_session_path_not_set_content(
    client, admin_user, project_with_doc, test_db, _clear_sessions,
):
    """With an active collab session, restore must route the content change through
    the session (apply_external_content_change) and NOT through ydoc_store.set_content.

    Why: set_content mutates a SEPARATE freshly-loaded Y.Doc and publishes its
    snapshot to the backplane. Combined with the session-doc broadcast, the two
    distinct client_ids emit conflicting CRDT histories and the restoring client
    diverges (visible-only-after-reload bug). The canonical pattern is or/else.
    """
    from unittest.mock import AsyncMock, patch

    from collab.registry import _get_or_create_session
    from helpers import set_session_text

    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "V1 restored body")
    await client.patch(f"/api/documents/{doc_id}", json={"content": "V2 edited body"}, cookies=cookies)

    # Register a live session for the doc so get_active_session finds it.
    session = await _get_or_create_session("doc", doc_id, "")
    set_session_text(session, "V2 edited body")

    with patch("ydoc_store.set_content", new_callable=AsyncMock) as mock_set_content:
        resp = await client.post(f"/api/checkpoints/{cp_id}/restore", cookies=cookies)

    assert resp.status_code == 200
    mock_set_content.assert_not_called()
    # Session Y.Doc must now reflect the restored content (single authoritative write).
    assert str(session._get_text()) == "V1 restored body"


# ─── C2: manual checkpoint content=None fallback reads the live Y.Doc ────────


@pytest.mark.asyncio
async def test_create_checkpoint_without_content_captures_live_tables(
    client, admin_user, project_with_doc, test_db, _clear_sessions,
):
    """POST /api/checkpoints with content=None must snapshot the live Y.Doc, not
    the GFM-derived documents.content.

    Why: a server-side create (no content sent) must produce a real restore point
    (raw anchor text + full tables state), so restoring it rebuilds identical
    editable table blocks. Falling back to documents.content stores GFM with tables
    inlined and tables_json=None → restore leaves orphan/duplicate-table state.
    """
    import json as _json

    from collab.registry import _get_or_create_session, _session_key, _sessions

    from table_serialize import capture_tables_json
    from ydoc_store import set_content

    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post("/api/documents", json={"project_id": pid, "title": "CpFallback"},
                             cookies=cookies)
    doc_id = resp.json()["document_id"]

    tables_payload = _json.dumps({"t1": {"columns": [160, 240], "rows": [["a", "b"], ["c", "d"]]}})
    # Put a table into the live Y.Doc via set_content (anchor text + tables subtree).
    await set_content(doc_id, "![t](table:t1)", persist=True, tables_json=tables_payload)
    # Register a live session so the live Y.Doc is the snapshot source.
    session = await _get_or_create_session("doc", doc_id, "![t](table:t1)")
    expected_tables = capture_tables_json(session.ydoc)
    _sessions.pop(_session_key("doc", doc_id), None)

    # Create checkpoint WITHOUT content (server-side snapshot).
    resp = await client.post(
        "/api/checkpoints",
        json={"document_id": doc_id, "label": "auto"},
        cookies=cookies,
    )
    assert resp.status_code == 200
    cp_id = resp.json()["checkpoint_id"]

    got = await client.get(f"/api/checkpoints/{cp_id}", cookies=cookies)
    body = got.json()
    # Raw anchor text (NOT GFM-expanded), full tables captured from the live Y.Doc.
    assert body["content"] == "![t](table:t1)"
    assert body["tables_json"] == expected_tables

    # Restore rebuilds the editable table subtree (round-trip).
    await client.patch(f"/api/documents/{doc_id}", json={"content": "changed"}, cookies=cookies)
    resp = await client.post(f"/api/checkpoints/{cp_id}/restore", cookies=cookies)
    assert resp.status_code == 200
    _sessions.pop(_session_key("doc", doc_id), None)
    from ydoc_store import load
    reloaded = await load(doc_id)
    assert _json.loads(capture_tables_json(reloaded)) == _json.loads(expected_tables)


# ─── F2: honest 502 when blob resolution fails on read ──────────────────────


async def _break_checkpoint_blob(test_db, cp_id):
    """Delete the cp_blobs row a checkpoint references, so resolution fails and
    (post-nullout) there is no inline content to fall back to."""
    rows = await test_db.query(
        "SELECT content_ref FROM type::record('checkpoints', $id)", {"id": cp_id},
    )
    ref = rows[0]["content_ref"] if rows else None
    if ref:
        await test_db.query("DELETE type::record('cp_blobs', $ref)", {"ref": ref})


async def _make_legacy_inline_checkpoint(test_db, cp_id, body="legacy body text"):
    """Strip the blob ref + hash and restore inline content (a pre-migration row)."""
    await test_db.query(
        "UPDATE type::record('checkpoints', $id) SET "
        "content = $c, content_ref = NONE, content_hash = NONE, tables_json = NONE, "
        "tables_hash = NONE",
        {"id": cp_id, "c": body},
    )


@pytest.mark.asyncio
async def test_get_checkpoint_502_when_blob_unreadable(client, admin_user, project_with_doc, test_db):
    """F2: a post-nullout row whose blob is gone has no inline fallback → 502,
    not a silently empty preview (no silent degradation)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "Body to preview")
    await _break_checkpoint_blob(test_db, cp_id)
    resp = await client.get(f"/api/checkpoints/{cp_id}", cookies=cookies)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "checkpoint_blob_unavailable"


@pytest.mark.asyncio
async def test_get_checkpoint_legacy_inline_still_200(client, admin_user, project_with_doc, test_db):
    """F2: the inline fallback is kept ONLY for legacy rows that actually have it."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "Pre-migration body")
    await _make_legacy_inline_checkpoint(test_db, cp_id, "legacy body text")
    resp = await client.get(f"/api/checkpoints/{cp_id}", cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["content"] == "legacy body text"


@pytest.mark.asyncio
async def test_patch_checkpoint_502_when_blob_unreadable(client, admin_user, project_with_doc, test_db):
    """F2: PATCH response resolves content from the blob — same 502 contract."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "Patch preview body")
    await _break_checkpoint_blob(test_db, cp_id)
    resp = await client.patch(
        f"/api/checkpoints/{cp_id}", json={"label": "renamed"}, cookies=cookies,
    )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "checkpoint_blob_unavailable"


@pytest.mark.asyncio
async def test_export_checkpoint_502_when_blob_unreadable(client, admin_user, project_with_doc, test_db):
    """F2: exporting a snapshot whose blob is gone → 502, not an empty file."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "Export me")
    await _break_checkpoint_blob(test_db, cp_id)
    resp = await client.get(
        f"/api/documents/{doc_id}/export?format=md&checkpoint_id={cp_id}", cookies=cookies,
    )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "checkpoint_blob_unavailable"


# ─── F4: distinguish transient blob failure (503) from corruption (409) ──────


@pytest.mark.asyncio
async def test_restore_503_when_blob_unreadable(client, admin_user, project_with_doc, test_db):
    """F4: a checkpoint whose blob cannot be read (transient/loss) must NOT be
    diagnosed as corruption. restore returns 503 (temporarily unavailable), and
    crucially must NOT fall through the legacy `valid is None → proceed` branch
    (blob_unreadable also returns valid: None)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "To restore")
    await client.patch(f"/api/documents/{doc_id}", json={"content": "Current live"}, cookies=cookies)
    await _break_checkpoint_blob(test_db, cp_id)

    resp = await client.post(f"/api/checkpoints/{cp_id}/restore", cookies=cookies)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "checkpoint_temporarily_unavailable"
    # Document untouched (the restore was aborted before any write).
    doc = await client.get(f"/api/documents/{doc_id}", cookies=cookies)
    assert doc.json()["content"] == "Current live"


@pytest.mark.asyncio
async def test_validate_returns_blob_unreadable_reason(client, admin_user, project_with_doc, test_db):
    """F4: the diagnostic GET /validate surfaces blob_unreadable as a reason in the
    body (valid: None) — NOT as an HTTP error (it is a diagnostic endpoint)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "Diagnostic body")
    await _break_checkpoint_blob(test_db, cp_id)

    resp = await client.get(f"/api/checkpoints/{cp_id}/validate", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is None
    assert body["reason"] == "blob_unreadable"
