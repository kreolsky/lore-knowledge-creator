"""Tests for chat session creation — inheritance of model / system_prompt_id
along the Reference → parent Document → defaults hierarchy.

Resolver runs at POST /api/chat/sessions when a field is omitted from the body.
Each field resolves independently.

# Plan "unify-agent-config": the `system_prompts_doc_id` field was removed (the
# prompt-folder subsystem is gone — personas live under `.lore/system`).
# Inheritance now covers model + system_prompt_id only."""


def _post_session(client, token, body):
    return client.post("/api/chat/sessions", json=body, cookies={"lore_session": token})


async def _create_ref(client, token, pid, document_id=None):
    body = {"project_id": pid, "title": "ref", "media_type": "markdown",
            "is_reference": True, "content": "x"}
    if document_id is not None:
        body["parent_id"] = document_id
    resp = await client.post("/api/documents", json=body, cookies={"lore_session": token})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


# ─── Case 8: parent_session_id overrides "latest" inheritance ─────────────────


async def test_parent_session_id_other_user_ignored(client, admin_user, regular_user, project_with_doc):
    """parent_session_id pointing at another user's chat must be ignored —
    fall back to latest-chat inheritance."""
    pid, doc_id, _ = project_with_doc
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )
    foreign = await _post_session(client, admin_token, {
        "project_id": pid, "document_id": doc_id,
        "model": "foreign-model", "system_prompt_id": "foreign-spid",
    })
    foreign_sid = foreign.json()["session_id"]
    # User has their own latest chat.
    await _post_session(client, user_token, {
        "project_id": pid, "document_id": doc_id,
        "model": "user-model",
    })
    resp = await _post_session(client, user_token, {
        "project_id": pid, "document_id": doc_id,
        "parent_session_id": foreign_sid,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == "user-model"
    assert data.get("system_prompt_id") in (None, "")


# ─── Case 9: PATCH then immediate create — inheritance sees PATCHed values ─


async def test_patched_system_prompt_inherits_to_ref_chat(client, admin_user, project_with_doc):
    """PATCH a system_prompt_id onto a doc-chat, then immediately create a ref-chat.
    The ref-chat must inherit the PATCHed system_prompt_id from the doc level."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, document_id=doc_id)
    # Create a doc chat with no system prompt initially.
    doc_sess = await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
    })
    doc_sid = doc_sess.json()["session_id"]
    # PATCH the doc chat to set a system_prompt_id.
    patch_resp = await client.patch(
        f"/api/chat/sessions/{doc_sid}",
        json={"system_prompt_id": "patched-spid"},
        cookies={"lore_session": token},
    )
    assert patch_resp.status_code == 200
    # Immediately create a ref chat — must inherit the PATCHed value.
    resp = await _post_session(client, token, {
        "project_id": pid, "reference_id": ref_id,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["system_prompt_id"] == "patched-spid"
