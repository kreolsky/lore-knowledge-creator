"""GET /chat/sessions/{id}/memory — the memory and the recall a chat received:
written once per persona, read back through the visibility wall."""

from routes.chat.completions_harness import _record_chat_block


async def _mk_chat(client, token, pid, doc_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


# TEST-WHY: refused-principal — a recorded chat the visibility wall no longer returns is left out of the read, a non-member is denied the record, and each record is written once per persona.
async def test_records_are_written_once_and_read_through_the_wall(
    client, admin_user, regular_user, project_with_doc, test_db,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    _, other_token = regular_user
    sid = await _mk_chat(client, token, pid, doc_id)
    kept = await _mk_chat(client, token, pid, doc_id)
    gone = await _mk_chat(client, token, pid, doc_id)
    await test_db.query(
        "UPDATE type::record('chat_sessions', $id) SET unindexed = true", {"id": gone})
    fresh = await client.get(f"/api/chat/sessions/{sid}/memory", cookies={"lore_session": token})
    assert fresh.status_code == 200 and fresh.json() == {"memory": None, "recall": None}

    recall = {"message_id": "u1", "persona_id": "P",
              "chats": [{"sid": kept, "date": "2026-11-20"}, {"sid": gone, "date": "2026-11-20"}]}
    stored = await _record_chat_block(sid, "recall", recall, None)
    again = await _record_chat_block(sid, "recall", recall | {"message_id": "u2"}, stored)
    assert again["message_id"] == "u1", "a second send of the same persona keeps the first record"
    switched = await _record_chat_block(sid, "recall", recall | {"persona_id": "Q"}, again)
    assert switched["persona_id"] == "Q", "a persona switch records its own recall"

    off = await _record_chat_block(
        sid, "memory", {"message_id": "u0", "chats": [], "error": "why", "persona_id": "Q"}, None)
    assert off["error"] == "why"
    on = await _record_chat_block(
        sid, "memory", {"message_id": "u1", "chats": [{"sid": kept, "date": "2026-11-19"}],
                        "error": None, "persona_id": "Q"}, off)
    assert on["message_id"] == "u1", "memory's errored record still rebuilds"

    resp = await client.get(f"/api/chat/sessions/{sid}/memory", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    memory, recall_read = resp.json()["memory"], resp.json()["recall"]
    assert [c["sid"] for c in recall_read["chats"]] == [kept], "an unindexed chat is left out"
    assert (recall_read["message_id"], recall_read["hidden"]) == ("u1", 1)
    assert recall_read["chats"][0]["title"] == (await _title(test_db, kept))
    assert [c["sid"] for c in memory["chats"]] == [kept] and (memory["hidden"], memory["error"]) == (0, None)

    resp = await client.get(
        f"/api/chat/sessions/{sid}/memory", cookies={"lore_session": other_token})
    assert resp.status_code == 404, "a non-member must be denied without learning the chat exists"


async def _title(test_db, sid):
    rows = await test_db.query(
        "SELECT title FROM type::record('chat_sessions', $id)", {"id": sid})
    return rows[0]["title"] or ""
