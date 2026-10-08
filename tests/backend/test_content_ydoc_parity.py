"""Every documents.content REST write carries ydoc_state (audit A4, plan step 4).

The CRDT is the editor's only write door; the REST arms that remain (the
no-session document PATCH, the reference content PATCH, the MCP markdown
upload backfill) must persist through ydoc_store.set_content(persist=True).
A raw `SET content` leaves the persisted Y.Doc at the OLD text while
documents.content says otherwise — the next collab open loads ydoc_state and
the browser shows the pre-PATCH body (silent split brain between the two
stores). With a registered zero-client session the PATCH must also CONVERGE
it: set_content publishes its snapshot to the backplane, which the session's
subscription applies (the no-session guard is LOCAL and checks clients, not
sessions).
"""

import asyncio
import hashlib
import secrets

import pytest

OLD = "The old body held by the persisted Y.Doc."
NEW = "The new body written through the REST arm."


async def _row(test_db, entity_id: str) -> dict:
    rows = await test_db.query(
        "SELECT content, ydoc_state FROM type::record('documents', $id)",
        {"id": entity_id},
    )
    return rows[0]


async def _ydoc_updates_count(test_db, entity_id: str) -> int:
    rows = await test_db.query(
        "SELECT count() AS total FROM ydoc_updates WHERE document_id = $id GROUP ALL",
        {"id": entity_id},
    )
    return rows[0].get("total", 0) if rows else 0


async def _wait_for(predicate, timeout_s: float = 3.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def _make_doc(client, cookies, pid: str) -> str:
    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": "Parity"}, cookies=cookies,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _make_agent_key(test_db, user_id, project_id) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"ul-key-{secrets.token_hex(4)}", {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "", "capabilities": ["agent"], "auto_apply": True,
    })
    return token


def _result_text(resp_json):
    import json

    return json.loads(resp_json["result"]["content"][0]["text"])


async def _call(client, token, name, arguments=None):
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments or {}}},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ─── document PATCH (no session) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_document_patch_carries_ydoc_state(client, admin_user, project_with_doc, test_db):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id = await _make_doc(client, cookies, pid)
    from ydoc_store import set_content

    await set_content(doc_id, OLD, persist=True)

    resp = await client.patch(
        f"/api/documents/{doc_id}", json={"content": NEW}, cookies=cookies,
    )
    assert resp.status_code == 200, resp.text

    row = await _row(test_db, doc_id)
    assert row["content"] == NEW
    from helpers import derive_content

    assert await derive_content(doc_id) == NEW, "ydoc_state still holds the OLD text"
    assert row["ydoc_state"]
    assert await _ydoc_updates_count(test_db, doc_id) == 0


# ─── reference PATCH (no session) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reference_patch_carries_ydoc_state(client, admin_user, project_with_doc, test_db):
    pid, host_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    ref_id = (await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": host_id, "title": "Parity ref",
              "media_type": "markdown", "content": ""},
        cookies=cookies,
    )).json()["reference_id"]
    from ydoc_store import set_content

    await set_content(ref_id, OLD, persist=True)

    resp = await client.patch(
        f"/api/references/{ref_id}", json={"content": NEW}, cookies=cookies,
    )
    assert resp.status_code == 200, resp.text

    row = await _row(test_db, ref_id)
    assert row["content"] == NEW
    from helpers import derive_content

    assert await derive_content(ref_id) == NEW, "ydoc_state still holds the OLD text"
    assert row["ydoc_state"]
    assert await _ydoc_updates_count(test_db, ref_id) == 0


# ─── MCP markdown upload backfill ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_markdown_upload_carries_ydoc_state(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)

    payload = _result_text(await _call(client, key, "attach_file", {
        "filename": "notes.md", "attach_to": doc_id, "title": "Notes",
    }))
    md = "# Agent notes\n\nBody text from the agent upload.\n".encode("utf-8")
    resp = await client.post(
        payload["url"], files={"file": ("notes.md", md, "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    ref_id = resp.json()["document_id"]

    from markdown_normalize import normalize_markdown

    normalized = normalize_markdown(md.decode("utf-8"))
    row = await _row(test_db, ref_id)
    assert row["content"] == normalized
    assert row["ydoc_state"], "markdown backfill left ydoc_state NONE"
    from helpers import derive_content

    assert await derive_content(ref_id) == normalized
    assert await _ydoc_updates_count(test_db, ref_id) == 0


# ─── a registered zero-client session converges ───────────────────────────────


@pytest.mark.asyncio
async def test_document_patch_converges_zero_client_session(
    client, admin_user, project_with_doc, test_db, _clear_sessions,
):
    """The no-session guard is LOCAL and checks clients: a registered session
    with ZERO clients still takes the PATCH, and the write must REACH it —
    set_content's backplane publish is applied by the session's subscription,
    so a client joining that session later sees the NEW text, not the OLD."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id = await _make_doc(client, cookies, pid)
    from helpers import derive_content

    from ydoc_store import set_content

    await set_content(doc_id, OLD, persist=True)
    from collab.registry import _get_or_create_session, get_active_session

    session = await _get_or_create_session("doc", doc_id, OLD)
    assert not session.clients
    assert session.content == OLD

    resp = await client.patch(
        f"/api/documents/{doc_id}", json={"content": NEW}, cookies=cookies,
    )
    assert resp.status_code == 200, resp.text

    assert await _wait_for(
        lambda: get_active_session("doc", doc_id).content == NEW,
    ), "PATCH never converged the registered zero-client session"
    assert await derive_content(doc_id) == NEW
