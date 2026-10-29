"""Contract tests for the unified placement + node-kind vocabulary.

Plan agent-document-placement-and-node-type:
  - `parent_id` is the ONLY placement name (create_document's `attach_to` and
    import_file's host `document_id` are deleted — one relation, one word);
  - `node_type` ("document" | "reference") is the ONLY kind name (the
    `is_reference` boolean argument is deleted from the tool surface);
  - create_document: `parent_id` is REQUIRED — omitted = 422 under an unscoped
    key (no silent project-root landing), explicit null = deliberate root;
    under a subtree-scoped key an omitted parent still resolves to the scope
    root (covered by test_subtree_scoped_agent_keys, kept green);
  - move_document: `node_type` omitted = KEEP the kind; passing it CONVERTS the
    node (doc→reference: sort_key cleared, after_id/root rejected, children
    refused; reference→doc: sort_key minted, file-backed refused).

The DB column `is_reference` is NOT part of this rename — rows still carry it,
and the `document_moved` broadcast carries it as the kind flag.
"""

import hashlib
import secrets

import pytest

# ─── Helpers (mirror test_append_move_tools) ─────────────────────────────────


async def _make_agent_key(test_db, user_id, project_id, *, auto_apply=True,
                          scope_root="", internal=False):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    key_id = f"nt-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id,
        "document_id": scope_root,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent", "capabilities": ["agent"],
        "auto_apply": auto_apply, "internal": internal,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


async def _make_doc(client, token, project_id, title, content="", parent_id=None):
    body = {"project_id": project_id, "title": title, "content": content}
    if parent_id is not None:
        body["parent_id"] = parent_id
    resp = await client.post("/api/documents", json=body,
                             cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _mk_ref(client, token, project_id, host, title):
    resp = await client.post(
        "/api/references",
        json={"project_id": project_id, "document_id": host, "title": title,
              "media_type": "markdown", "content": "body"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["reference_id"]


async def _row(test_db, doc_id):
    rows = await test_db.query(
        "SELECT is_reference, parent_id, sort_key, file_path, media_type, title "
        "FROM type::record('documents', $id)",
        {"id": doc_id},
    )
    assert rows, f"row {doc_id} missing"
    from db import extract_id

    row = dict(rows[0])
    row["parent_id"] = extract_id(row["parent_id"]) if row.get("parent_id") else None
    return row


# ═══ create_document — placement is required; node_type names the kind ═══════


# ═══ import_file — host is parent_id; kind is node_type ══════════════════════


# ═══ move_document — node_type converts in both directions ═══════════════════


@pytest.mark.asyncio
async def test_move_converts_document_to_reference(
    client, test_db, admin_user, project_with_doc,
):
    """"take it and make it a reference" — one structural op, not a third node:
    is_reference=true, parent=host, sort_key = the TOP key of the host's
    reference group (the old tree key must not survive the conversion — a kept
    key leaves the node in tree sibling queries and it renders twice)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "Host")
    child = await _make_doc(client, token, pid, "Child", parent_id=host)
    # An existing ref on the host pins the ref group: the converted node must
    # land at its TOP (key below the existing ref's key), not keep its tree key.
    existing_ref = await _mk_ref(client, token, pid, host, "Existing")
    tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/api/tool/move_document", json={
        "document_id": child, "parent_id": host, "node_type": "reference",
        "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"
    row = await _row(test_db, child)
    assert row["is_reference"] is True
    assert row["parent_id"] == host
    assert isinstance(row["sort_key"], str) and row["sort_key"]
    existing_key = (await _row(test_db, existing_ref))["sort_key"]
    assert row["sort_key"] < existing_key, "conversion lands at the group's top"
    # The node left the tree listing (the project doc list is non-reference).
    listing = await client.get(
        f"/api/projects/{pid}", cookies={"lore_session": token},
    )
    ids = {d["document_id"] for d in listing.json()["documents"]}
    assert child not in ids


# ═══ document_moved broadcast carries the kind ═══════════════════════════════


# ═══ served schemas speak the new vocabulary only ════════════════════════════
