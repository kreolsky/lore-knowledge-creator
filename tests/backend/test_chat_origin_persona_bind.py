"""The persona PATCH is the memory binding: `system_prompt_id` re-binds `origin_persona_id`."""


async def _mk_chat(client, token, pid, doc_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def _patch(client, token, sid, body):
    return await client.patch(
        f"/api/chat/sessions/{sid}", json=body, cookies={"lore_session": token},
    )


async def _origin(test_db, sid):
    rows = await test_db.query(
        "SELECT origin_persona_id FROM type::record('chat_sessions', $id)", {"id": sid})
    return rows[0].get("origin_persona_id")


# TEST-WHY: refused-principal — another full member of the project cannot re-bind the owner's AI chat.
async def test_non_owner_cannot_bind(client, collab_project, test_db):
    pid, doc_id, admin_token, user_token, _, _ = collab_project
    sid = await _mk_chat(client, admin_token, pid, doc_id)
    resp = await _patch(client, user_token, sid, {"system_prompt_id": None})
    assert resp.status_code == 404, "a non-owner must be denied without learning the chat exists"
    assert await _origin(test_db, sid) is None


# TEST-WHY: cross-process-contract — the persona PATCH stores the binding the worker's labeller and the turn's stamp read for the whole conversation.
async def test_default_binds_whole_conversation_and_survives_stamp(
    client, admin_user, project_with_doc, test_db, enqueue_recorder,
):
    from routes.chat.completions_harness import _stamp_origin_persona

    pid, doc_id, uid = project_with_doc
    _, token = admin_user
    root = await _mk_chat(client, token, pid, doc_id)
    base = {"title": "", "model": "m", "project_id": pid, "user_id": uid}
    await test_db.query(
        "CREATE type::record('chat_sessions', 'b1') CONTENT $b; "
        "CREATE type::record('chat_sessions', 'c1') CONTENT $c; "
        "CREATE type::record('chat_tocs', 'c1') CONTENT $t",
        {"b": {**base, "thread_id": root}, "c": {**base, "compacted_from": "b1"},
         "t": {"user_id": uid, "project_id": pid, "chat_id": "c1",
               "model": "m", "frozen_ids": [], "labelled_ids": [], "labelled_hashes": [],
               "topics": [{"title": "kept", "synopsis": "", "message_ids": [], "frozen": False}]}},
    )

    resp = await _patch(client, token, root, {"system_prompt_id": "p1"})
    assert resp.status_code == 200, resp.text
    assert [await _origin(test_db, s) for s in (root, "b1", "c1")] == ["p1"] * 3
    assert enqueue_recorder.of("chat_topics_pair_task")

    resp = await _patch(client, token, root, {"system_prompt_id": None})
    assert resp.status_code == 200, resp.text
    assert [await _origin(test_db, s) for s in (root, "b1", "c1")] == [""] * 3
    toc = (await test_db.query("SELECT topics FROM type::record('chat_tocs', 'c1')"))[0]
    assert toc["topics"][0]["title"] == "kept"

    await _stamp_origin_persona(root, "p-new")
    assert await _origin(test_db, root) == ""
