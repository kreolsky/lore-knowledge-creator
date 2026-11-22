"""`move_chat` — the agent's reparent of the user's own chat (the `structure`
skill's pack).

Every case here drives the REFUSED principal: the row that must not move sits
in the DB, so only the handler's ownership predicate and the in-app-only gate
stand between the caller and the write. The write itself is the PATCH path's
command — its target guards are covered by test_chat_reparent.py.
"""
import hashlib
import secrets

import pytest


@pytest.fixture
async def world(client, test_db, admin_user, regular_user, project_with_doc):
    """Project P with members A (admin) and B, and an internal agent key for each."""
    pid, _, a_uid = project_with_doc
    b_uid = regular_user[0]
    await test_db.query(
        "CREATE type::record('project_members', $id) SET project_id = $pid, "
        "user_id = $uid, access_level = 'full'",
        {"id": f"pm-{pid}-{b_uid}", "pid": pid, "uid": b_uid},
    )
    return {"pid": pid, "a": a_uid, "b": b_uid,
            "ka": await _key(a_uid, pid), "kb": await _key(b_uid, pid)}


async def _key(user_id: str, project_id: str, *, internal: bool = True) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"agent-key-{secrets.token_hex(4)}", {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent", "capabilities": ["agent"], "internal": internal,
    })
    return token


async def _session(db, sid, uid, pid, **fields):
    await db.query(
        "CREATE type::record('chat_sessions', $id) CONTENT $f",
        {"id": sid, "f": {"user_id": uid, "project_id": pid, "title": f"title {sid}",
                          "model": "m", **fields}},
    )


async def _doc(pid, did, parent=None, **fields):
    from db import create_record

    await create_record("documents", did, {
        "project_id": pid, "parent_id": parent, "title": f"doc {did}", "content": "",
        "path": f"{did}.md", "is_index": False, **fields,
    })


async def _post(client, token, body, session_id=None, status=200) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if session_id:
        headers["X-Agent-Session-Id"] = session_id
    resp = await client.post("/api/tool/move_chat", json=body, headers=headers)
    assert resp.status_code == status, resp.text
    return resp.json()


async def _parents(db, *ids: str) -> dict[str, str | None]:
    rows = await db.query(
        "SELECT meta::id(id) AS id, document_id FROM chat_sessions "
        "WHERE meta::id(id) IN $ids", {"ids": list(ids)})
    return {r["id"]: r["document_id"] for r in rows}


# TEST-WHY: refused-principal — another user's chat id, a key that is not the in-app assistant's, and a note must not move a chat; each row sits in the DB, so only the handler's own gates refuse (404 / 403 / 400)
async def test_move_chat_refuses_foreign_chat_external_key_and_note(client, test_db, world):
    """B's key naming A's chat → 404, the same answer as a chat id that does
    not exist (the id confirms nothing); A's own user-minted EXTERNAL key →
    403; a call with no chat id anywhere → 404; a note → 400 BEFORE the hold —
    the 409 a confirm-mode call gets once it passes the gates. Every row keeps
    its parent."""
    pid, a, b = world["pid"], world["a"], world["b"]
    await _doc(pid, "dA")
    await _doc(pid, "dB")
    await _session(test_db, "chatA", a, pid, document_id="dA")
    await _session(test_db, "noteB", b, pid, is_note=True, document_id="dA")
    await _session(test_db, "chatB", b, pid, document_id="dB")

    refused = await _post(client, world["kb"], {"chat_id": "chatA", "document_id": None},
                          session_id="chatB", status=404)
    assert refused["detail"] == "Chat not found"
    refused = await _post(client, world["kb"], {"document_id": None}, status=404)
    assert refused["detail"] == "Chat not found"
    external = await _key(a, pid, internal=False)
    refused = await _post(client, external, {"chat_id": "chatA", "document_id": None}, status=403)
    assert refused["detail"] == "this tool is available only to the in-app assistant"
    refused = await _post(client, world["kb"], {"chat_id": "noteB", "document_id": "dB"}, status=400)
    assert refused["detail"] == "Cannot change parent of a note session"

    assert await _parents(test_db, "chatA", "noteB", "chatB") == {
        "chatA": "dA", "noteB": "dA", "chatB": "dB",
    }
