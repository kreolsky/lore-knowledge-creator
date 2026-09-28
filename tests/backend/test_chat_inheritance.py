"""Tests for chat session creation — inheritance of model / system_prompt_id
along the Reference → parent Document → defaults hierarchy.

Resolver runs at POST /api/chat/sessions when a field is omitted from the body.
Each field resolves independently.

# Plan "unify-agent-config": the `system_prompts_doc_id` field was removed (the
# prompt-folder subsystem is gone — personas live under `.lore/system`).
# Inheritance now covers model + system_prompt_id only."""


from config import CHAT_MODEL


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


# ─── Case 1: explicit fields are respected (no inheritance overrides body) ──


async def test_explicit_fields_respected(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    # Seed a previous chat with different settings to ensure body wins.
    await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id, "model": "prev-model",
        "system_prompt_id": "prev-spid",
    })
    resp = await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id, "model": "explicit-model",
        "system_prompt_id": "explicit-spid",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == "explicit-model"
    assert data["system_prompt_id"] == "explicit-spid"


# ─── Case 2: new ref-chat inherits from latest ref-chat ──────────────────────


async def test_inherit_from_latest_reference_chat(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, document_id=doc_id)
    # First ref-chat with explicit settings.
    await _post_session(client, token, {
        "project_id": pid, "reference_id": ref_id,
        "model": "ref-model", "system_prompt_id": "ref-spid",
    })
    # New ref-chat with all inheritance fields omitted.
    resp = await _post_session(client, token, {
        "project_id": pid, "reference_id": ref_id,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == "ref-model"
    assert data["system_prompt_id"] == "ref-spid"


# ─── Case 3: new ref-chat with no ref-chats inherits from parent doc ────────


async def test_inherit_from_parent_document_chat(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, document_id=doc_id)
    # Doc-level chat first.
    await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
        "model": "doc-model", "system_prompt_id": "doc-spid",
    })
    # First chat for the reference — no ref-chats exist yet.
    resp = await _post_session(client, token, {
        "project_id": pid, "reference_id": ref_id,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == "doc-model"
    assert data["system_prompt_id"] == "doc-spid"


# ─── Case 4: ref with NULL document_id falls back to global defaults ────────


async def test_orphan_reference_falls_back_to_defaults(client, admin_user, project_with_doc):
    # A reference whose host has no chat config resolves to defaults. Hosted on the
    # project index doc (a no-host reference is now impossible — reference-host invariant).
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, document_id=idx_id)
    resp = await _post_session(client, token, {
        "project_id": pid, "reference_id": ref_id,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == CHAT_MODEL
    assert data.get("system_prompt_id") in (None, "")


# ─── Case 5: ref attached to doc but no chats anywhere → defaults ───────────


async def test_no_chats_anywhere_falls_back_to_defaults(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, document_id=doc_id)
    resp = await _post_session(client, token, {
        "project_id": pid, "reference_id": ref_id,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == CHAT_MODEL
    assert data.get("system_prompt_id") in (None, "")


# ─── Case 6: doc-only — new doc-chat inherits from prior doc-chat ───────────


async def test_doc_chat_inherits_from_prior_doc_chat(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
        "model": "doc-model-A", "system_prompt_id": "doc-spid-A",
    })
    resp = await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == "doc-model-A"
    assert data["system_prompt_id"] == "doc-spid-A"


# ─── Case 7: per-field independence — fall through nulls per field ──────────


async def test_fields_resolve_from_single_project_latest_row(client, admin_user, project_with_doc):
    """Decision 2: inheritance is PROJECT-LATEST per field — each omitted field is
    read from the single latest AI chat in the project (ORDER BY updated_at DESC).
    Both fields therefore come from the SAME latest row; the old per-scope-level
    fall-through (ref model, doc system_prompt) is gone."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, document_id=doc_id)
    # Older chat: model='A', system_prompt_id='P1' on the document.
    await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
        "model": "A", "system_prompt_id": "P1",
    })
    # Latest chat: model='B', system_prompt_id='P2' on the reference.
    await _post_session(client, token, {
        "project_id": pid, "reference_id": ref_id, "model": "B", "system_prompt_id": "P2",
    })
    # New chat with all fields omitted.
    resp = await _post_session(client, token, {
        "project_id": pid, "reference_id": ref_id,
    })
    assert resp.status_code == 201
    data = resp.json()
    # Both fields come from the SAME latest row (the ref-chat): model=B, prompt=P2.
    # Under the old per-scope-level walk model would be B but prompt P1 (doc level).
    assert data["model"] == "B"
    assert data["system_prompt_id"] == "P2"


async def test_inheritance_is_project_wide(client, admin_user, project_with_doc):
    """Decision 2: a new AI chat inherits model from the latest AI chat ANYWHERE in
    the project — not just the same document/reference branch. A donor on a different
    document donates its model."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    doc_b = await _create_ref(client, token, pid, document_id=doc_id)
    # Chat on doc B with an explicit model (the donor).
    await _post_session(client, token, {
        "project_id": pid, "document_id": doc_b, "model": "cross-doc-model",
    })
    # A new chat on the original doc inherits the cross-doc model.
    resp = await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
    })
    assert resp.status_code == 201
    assert resp.json()["model"] == "cross-doc-model"


async def test_inheritance_ignores_note_sessions(client, admin_user, project_with_doc):
    """Decision 2: the project-latest walk adds is_note=false so a note session
    (which would otherwise carry model='') can never donate to a new AI chat."""
    from db import create_record, get_db
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    # Insert a note session with a non-empty model directly (notes never set model
    # via the create path, so simulate a dirty row to prove the filter).
    db = await get_db()
    note_sid = "test-inherit-note-1"
    await create_record("chat_sessions", note_sid, {
        "project_id": pid, "user_id": admin_user[0], "document_id": doc_id,
        "title": "", "model": "note-poison", "is_note": True,
    })
    try:
        resp = await _post_session(client, token, {
            "project_id": pid, "document_id": doc_id,
        })
        assert resp.status_code == 201
        # Falls back to the default model (CHAT_MODEL), NOT the note's poison value.
        assert resp.json()["model"] == CHAT_MODEL
    finally:
        await db.query("DELETE type::record('chat_sessions', $id)", {"id": note_sid})


# ─── Case 8: parent_session_id overrides "latest" inheritance ─────────────────


async def test_parent_session_id_inherits_from_specified_chat(client, admin_user, project_with_doc):
    """parent_session_id must take precedence over latest-chat lookup —
    even when there's a more recent chat with different settings."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    older = await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
        "model": "older-model", "system_prompt_id": "older-spid",
    })
    older_sid = older.json()["session_id"]
    # Make a more recent chat — would normally be the inheritance source.
    await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
        "model": "newer-model", "system_prompt_id": "newer-spid",
    })
    # New chat asks to inherit from the older one explicitly.
    resp = await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
        "parent_session_id": older_sid,
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["model"] == "older-model"
    assert data["system_prompt_id"] == "older-spid"


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


async def test_parent_session_id_deleted_ignored(client, admin_user, project_with_doc):
    """parent_session_id pointing at a soft-deleted session must be ignored."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    older = await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
        "model": "deleted-model",
    })
    older_sid = older.json()["session_id"]
    await client.delete(f"/api/chat/sessions/{older_sid}", cookies={"lore_session": token})
    # Surviving chat with different model.
    await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
        "model": "surviving-model",
    })
    resp = await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
        "parent_session_id": older_sid,
    })
    assert resp.status_code == 201
    assert resp.json()["model"] == "surviving-model"


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


async def test_patched_model_inherits_to_ref_chat(client, admin_user, project_with_doc):
    """PATCH a model onto a doc-chat, then immediately create a ref-chat.
    The ref-chat must inherit the PATCHed model from the doc level."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, document_id=doc_id)
    doc_sess = await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
    })
    doc_sid = doc_sess.json()["session_id"]
    patch_resp = await client.patch(
        f"/api/chat/sessions/{doc_sid}",
        json={"model": "patched-model"},
        cookies={"lore_session": token},
    )
    assert patch_resp.status_code == 200
    resp = await _post_session(client, token, {
        "project_id": pid, "reference_id": ref_id,
    })
    assert resp.status_code == 201
    assert resp.json()["model"] == "patched-model"


# ─── Case 10: explicit null overrides inheritance (not "null == inherit") ────


async def test_explicit_null_system_prompt_means_no_prompt(client, admin_user, project_with_doc):
    """Explicit null means 'Default'/no prompt — must NOT inherit the previous
    chat's system_prompt_id."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    # Seed a previous chat with a system prompt.
    await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
        "system_prompt_id": "inherited-spid",
    })
    # New chat explicitly picks Default (system_prompt_id: null).
    resp = await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
        "system_prompt_id": None,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data.get("system_prompt_id") in (None, ""), \
        f"expected null/empty system_prompt_id, got {data.get('system_prompt_id')!r}"


async def test_omitted_system_prompt_still_inherits(client, admin_user, project_with_doc):
    """Omitted field (not in body at all) still inherits — regression guard for
    the null-vs-absent distinction."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
        "system_prompt_id": "inherited-spid",
    })
    resp = await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
    })
    assert resp.status_code == 201
    assert resp.json()["system_prompt_id"] == "inherited-spid"
