"""Integration tests for checkpoint routes."""

import pytest


@pytest.mark.asyncio
async def test_create_checkpoint(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "CpDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    # Set content
    await client.patch(
        f"/api/documents/{doc_id}",
        json={"content": "Version 1"},
        cookies={"lore_session": token},
    )
    resp = await client.post(
        "/api/checkpoints",
        json={"document_id": doc_id, "label": "v1", "comment": "First version"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["label"] == "v1"
    assert data["content"] == "Version 1"


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
async def test_restore_checkpoint(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "RestoreDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    # Set v1 content
    await client.patch(
        f"/api/documents/{doc_id}",
        json={"content": "V1 content"},
        cookies={"lore_session": token},
    )
    # Create checkpoint
    resp = await client.post(
        "/api/checkpoints",
        json={"document_id": doc_id, "label": "v1"},
        cookies={"lore_session": token},
    )
    cp_id = resp.json()["checkpoint_id"]
    # Change to v2
    await client.patch(
        f"/api/documents/{doc_id}",
        json={"content": "V2 content"},
        cookies={"lore_session": token},
    )
    # Restore v1
    resp = await client.post(
        f"/api/checkpoints/{cp_id}/restore",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    # Verify document has v1 content
    resp = await client.get(f"/api/documents/{doc_id}", cookies={"lore_session": token})
    assert resp.json()["content"] == "V1 content"
    # Verify _backup was created
    resp = await client.get(
        f"/api/checkpoints?document_id={doc_id}",
        cookies={"lore_session": token},
    )
    labels = [cp["label"] for cp in resp.json()]
    assert "_backup" in labels


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
async def test_restore_rejects_corrupted_checkpoint(client, admin_user, project_with_doc, test_db):
    """A checkpoint whose content no longer matches its stored hash must not be
    restored — restore returns 409 and the document content stays unchanged.

    Phase 1: content lives in cp_blobs (content_ref), so we tamper the blob data
    to simulate storage corruption.
    """
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "V1 content here")

    # Move document to V2 so we can assert it is untouched after a failed restore.
    await client.patch(f"/api/documents/{doc_id}", json={"content": "V2 content now"}, cookies=cookies)

    # Find the blob ref for this checkpoint and tamper the blob data.
    cp_rows = await test_db.query(
        "SELECT content_ref FROM type::record('checkpoints', $id)",
        {"id": cp_id},
    )
    content_ref = cp_rows[0]["content_ref"] if cp_rows else None

    if content_ref:
        import zstandard
        corrupted = zstandard.compress(b"corrupted bytes")
        await test_db.query(
            "UPDATE type::record('cp_blobs', $ref) SET data = $bad",
            {"ref": content_ref, "bad": corrupted},
        )
    else:
        # Legacy row without blob ref — tamper inline content.
        await test_db.query(
            "UPDATE type::record('checkpoints', $id) SET content = $c",
            {"id": cp_id, "c": "corrupted bytes"},
        )

    resp = await client.post(f"/api/checkpoints/{cp_id}/restore", cookies=cookies)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "checkpoint_corrupted"

    doc = await client.get(f"/api/documents/{doc_id}", cookies=cookies)
    assert doc.json()["content"] == "V2 content now"


@pytest.mark.asyncio
async def test_restore_allows_legacy_no_hash(client, admin_user, project_with_doc, test_db):
    """A legacy checkpoint with no stored content_hash restores normally."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "Legacy content body")

    await client.patch(f"/api/documents/{doc_id}", json={"content": "Changed away"}, cookies=cookies)
    await test_db.query(
        "UPDATE type::record('checkpoints', $id) SET content_hash = NONE",
        {"id": cp_id},
    )

    resp = await client.post(f"/api/checkpoints/{cp_id}/restore", cookies=cookies)
    assert resp.status_code == 200

    doc = await client.get(f"/api/documents/{doc_id}", cookies=cookies)
    assert doc.json()["content"] == "Legacy content body"


@pytest.mark.asyncio
async def test_validate_endpoint_reports_states(client, admin_user, project_with_doc, test_db):
    """GET /validate returns valid:true for an intact checkpoint, valid:false for a tampered one."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "Intact content body")

    resp = await client.get(f"/api/checkpoints/{cp_id}/validate", cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True

    # Tamper the blob data to simulate corruption
    cp_rows = await test_db.query(
        "SELECT content_ref FROM type::record('checkpoints', $id)",
        {"id": cp_id},
    )
    content_ref = cp_rows[0]["content_ref"] if cp_rows else None

    if content_ref:
        import zstandard
        corrupted = zstandard.compress(b"flipped")
        await test_db.query(
            "UPDATE type::record('cp_blobs', $ref) SET data = $bad",
            {"ref": content_ref, "bad": corrupted},
        )
    else:
        await test_db.query(
            "UPDATE type::record('checkpoints', $id) SET content = $c",
            {"id": cp_id, "c": "flipped"},
        )
    resp = await client.get(f"/api/checkpoints/{cp_id}/validate", cookies=cookies)
    assert resp.json()["valid"] is False


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


# ─── PATCH /api/checkpoints/{checkpoint_id} ─────────────────────────────────


@pytest.mark.asyncio
async def test_patch_checkpoint_label(client, admin_user, project_with_doc):
    """Update checkpoint label → verify via list."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "PatchCpDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/checkpoints",
        json={"document_id": doc_id, "label": "old-label"},
        cookies={"lore_session": token},
    )
    cp_id = resp.json()["checkpoint_id"]
    resp = await client.patch(
        f"/api/checkpoints/{cp_id}",
        json={"label": "new-label"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    # Verify label changed
    resp = await client.get(
        f"/api/checkpoints?document_id={doc_id}",
        cookies={"lore_session": token},
    )
    cp = next(c for c in resp.json() if c["checkpoint_id"] == cp_id)
    assert cp["label"] == "new-label"


@pytest.mark.asyncio
async def test_patch_checkpoint_comment(client, admin_user, project_with_doc):
    """Update checkpoint comment only."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "CommentCpDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/checkpoints",
        json={"document_id": doc_id, "label": "v1"},
        cookies={"lore_session": token},
    )
    cp_id = resp.json()["checkpoint_id"]
    resp = await client.patch(
        f"/api/checkpoints/{cp_id}",
        json={"comment": "Important checkpoint"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    resp = await client.get(
        f"/api/checkpoints?document_id={doc_id}",
        cookies={"lore_session": token},
    )
    cp = next(c for c in resp.json() if c["checkpoint_id"] == cp_id)
    assert cp["comment"] == "Important checkpoint"
    assert cp["label"] == "v1"  # label unchanged


@pytest.mark.asyncio
async def test_patch_checkpoint_both(client, admin_user, project_with_doc):
    """Update both label and comment simultaneously."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "BothCpDoc"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/checkpoints",
        json={"document_id": doc_id, "label": "old"},
        cookies={"lore_session": token},
    )
    cp_id = resp.json()["checkpoint_id"]
    resp = await client.patch(
        f"/api/checkpoints/{cp_id}",
        json={"label": "renamed", "comment": "Added comment"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    resp = await client.get(
        f"/api/checkpoints?document_id={doc_id}",
        cookies={"lore_session": token},
    )
    cp = next(c for c in resp.json() if c["checkpoint_id"] == cp_id)
    assert cp["label"] == "renamed"
    assert cp["comment"] == "Added comment"


@pytest.mark.asyncio
async def test_list_checkpoints_includes_user_name(client, admin_user, project_with_doc):
    """list_checkpoints resolves created_by → user_name; autos without created_by show 'System'."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": "UserNameDoc"}, cookies=cookies,
    )
    doc_id = resp.json()["document_id"]
    await client.patch(f"/api/documents/{doc_id}", json={"content": "V1"}, cookies=cookies)

    # Manual checkpoint
    resp = await client.post(
        "/api/checkpoints", json={"document_id": doc_id, "label": "v1"}, cookies=cookies,
    )
    assert resp.status_code == 200

    # List — manual checkpoint must have user_name matching the creator
    resp = await client.get(f"/api/checkpoints?document_id={doc_id}", cookies=cookies)
    assert resp.status_code == 200
    rows = resp.json()
    manual = [r for r in rows if r["label"] == "v1"]
    assert len(manual) == 1
    assert manual[0]["user_name"] == "testadmin"
    assert manual[0]["created_by"] == "test-admin-001"

    # Restore creates a _backup checkpoint (no created_by) — should resolve to "System"
    cp_id = manual[0]["checkpoint_id"]
    await client.patch(f"/api/documents/{doc_id}", json={"content": "V2"}, cookies=cookies)
    resp = await client.post(f"/api/checkpoints/{cp_id}/restore", cookies=cookies)
    assert resp.status_code == 200

    resp = await client.get(f"/api/checkpoints?document_id={doc_id}", cookies=cookies)
    rows = resp.json()
    backup = [r for r in rows if r["label"] == "_backup"]
    assert len(backup) == 1
    assert backup[0]["user_name"] == "System"


@pytest.mark.asyncio
async def test_get_and_patch_checkpoint_preserve_user_name(client, admin_user, project_with_doc):
    """GET /{id} and PATCH /{id} must also resolve user_name (regression: preview/edit showed 'System')."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": "PreserveNameDoc"}, cookies=cookies,
    )
    doc_id = resp.json()["document_id"]
    await client.patch(f"/api/documents/{doc_id}", json={"content": "V1"}, cookies=cookies)
    resp = await client.post(
        "/api/checkpoints", json={"document_id": doc_id, "label": "v1"}, cookies=cookies,
    )
    cp_id = resp.json()["checkpoint_id"]

    # Single GET (preview fetch) must carry the name, not fall back to System.
    resp = await client.get(f"/api/checkpoints/{cp_id}", cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["user_name"] == "testadmin"

    # PATCH (edit comment) response must carry the name too.
    resp = await client.patch(f"/api/checkpoints/{cp_id}", json={"comment": "note"}, cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["user_name"] == "testadmin"


@pytest.mark.asyncio
async def test_export_with_checkpoint_id_returns_snapshot_content(client, admin_user, project_with_doc):
    """Exporting with checkpoint_id returns the SNAPSHOT content, not the live
    document — the whole point of the green Download button while a snapshot
    preview is open. Without checkpoint_id, the live document is exported."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post("/api/documents", json={"project_id": pid, "title": "SnapExport"}, cookies=cookies)
    doc_id = resp.json()["document_id"]
    await client.patch(f"/api/documents/{doc_id}", json={"content": "Version 1"}, cookies=cookies)

    cp = await client.post(
        "/api/checkpoints",
        json={"document_id": doc_id, "label": "v1"},
        cookies=cookies,
    )
    assert cp.status_code == 200
    cp_id = cp.json()["checkpoint_id"]

    # Mutate the live document AFTER the snapshot was taken.
    await client.patch(f"/api/documents/{doc_id}", json={"content": "Version 2"}, cookies=cookies)

    # Export the snapshot — must be "Version 1".
    snap_md = await client.get(
        f"/api/documents/{doc_id}/export?format=md&checkpoint_id={cp_id}", cookies=cookies,
    )
    assert snap_md.status_code == 200
    assert snap_md.text == "Version 1"

    # Export the live document — must be "Version 2".
    live_md = await client.get(f"/api/documents/{doc_id}/export?format=md", cookies=cookies)
    assert live_md.status_code == 200
    assert live_md.text == "Version 2"


@pytest.mark.asyncio
async def test_export_with_foreign_checkpoint_id_404s(client, admin_user, project_with_doc):
    """A checkpoint_id belonging to a different document must NOT be exported
    via this document's export route (cross-doc leak guard)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    a = await client.post("/api/documents", json={"project_id": pid, "title": "DocA"}, cookies=cookies)
    doc_a = a.json()["document_id"]
    b = await client.post("/api/documents", json={"project_id": pid, "title": "DocB"}, cookies=cookies)
    doc_b = b.json()["document_id"]

    await client.patch(f"/api/documents/{doc_a}", json={"content": "A"}, cookies=cookies)
    cp = await client.post("/api/checkpoints", json={"document_id": doc_a, "label": "a"}, cookies=cookies)
    cp_id = cp.json()["checkpoint_id"]

    # Try to export DocA's checkpoint through DocB's route.
    resp = await client.get(
        f"/api/documents/{doc_b}/export?format=md&checkpoint_id={cp_id}", cookies=cookies,
    )
    assert resp.status_code == 404


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


# ─── T2: shared restore field-set builder ────────────────────────────────────


@pytest.mark.asyncio
async def test_backup_row_carries_full_field_set(client, admin_user, project_with_doc, test_db):
    """The before-restore _backup row must carry the SAME field set as a manual
    checkpoint (content_ref, content_hash, tables_json, tables_hash) — both writers
    consume the shared cp_store.checkpoint_row_fields builder. A field added to the
    builder appears in the _backup row; the restore path no longer hand-builds the list.
    """
    from cp_store import checkpoint_row_fields

    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post("/api/documents", json={"project_id": pid, "title": "T2Builder"},
                             cookies=cookies)
    doc_id = resp.json()["document_id"]
    await client.patch(f"/api/documents/{doc_id}", json={"content": "V1"}, cookies=cookies)
    cp = await client.post("/api/checkpoints", json={"document_id": doc_id, "label": "v1"},
                           cookies=cookies)
    cp_id = cp.json()["checkpoint_id"]

    # Mutate then restore → triggers the _backup row write through the shared builder.
    await client.patch(f"/api/documents/{doc_id}", json={"content": "V2"}, cookies=cookies)
    resp = await client.post(f"/api/checkpoints/{cp_id}/restore", cookies=cookies)
    assert resp.status_code == 200

    backup_rows = await test_db.query(
        "SELECT content_ref, content_hash, tables_json, tables_hash, label, comment, "
        "created_by FROM checkpoints WHERE document_id = $did AND label = '_backup'",
        {"did": doc_id},
    )
    assert backup_rows, "expected a _backup row after restore"
    b = backup_rows[0]
    assert b["content_ref"]
    assert b["content_hash"] == b["content_ref"]
    assert b["tables_json"] is not None  # captured from the live Y.Doc
    assert b["tables_hash"]
    assert b["comment"] == "Auto-backup before restore"
    assert b["created_by"] is None

    # The builder's keys are exactly the row-identity + blob fields written above.
    built = await checkpoint_row_fields(
        "V2", b["tables_json"], document_id=doc_id, label="_backup",
        comment="Auto-backup before restore", created_by=None,
    )
    assert set(built.keys()) == {
        "document_id", "label", "comment", "created_by",
        "content_ref", "content_hash", "tables_json", "tables_hash",
    }


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

