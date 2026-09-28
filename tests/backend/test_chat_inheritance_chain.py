"""Tests for the 4-rung inheritance chain (Spec §4.2).

Each new chat resolves missing fields (model, system_prompt_id)
via: parent_session → latest on same scope → latest on parent doc (for refs) → project defaults.
Each field walks the chain INDEPENDENTLY — first hit per field.

# Plan "unify-agent-config": `system_prompts_doc_id` removed from the chain.
"""


from config import CHAT_MODEL


def _post_session(client, token, body):
    return client.post("/api/chat/sessions", json=body, cookies={"lore_session": token})


async def _create_ref(client, token, pid, document_id):
    resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid,
            "parent_id": document_id,
            "title": "ref",
            "media_type": "markdown",
            "is_reference": True,
            "content": "x",
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


async def test_inherits_from_parent_session_when_provided(client, admin_user, project_with_doc):
    """Rung 1: parent_session_id wins for model + system_prompt_id."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    parent = (await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id, "model": "parent-model",
        "system_prompt_id": "parent-spid",
    })).json()
    resp = await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id, "parent_session_id": parent["session_id"],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == "parent-model"
    assert data["system_prompt_id"] == "parent-spid"


async def test_inherits_from_latest_on_same_ref_when_no_parent_session(client, admin_user, project_with_doc):
    """Rung 2: newest session on the same ref-doc supplies missing fields."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, doc_id)
    # Seed parent doc with different model so we can verify ref wins.
    await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id, "model": "doc-model",
        "system_prompt_id": "doc-spid",
    })
    await _post_session(client, token, {
        "project_id": pid, "reference_id": ref_id, "model": "ref-model",
        "system_prompt_id": "ref-spid",
    })
    resp = await _post_session(client, token, {"project_id": pid, "reference_id": ref_id})
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == "ref-model"
    assert data["system_prompt_id"] == "ref-spid"


async def test_inherits_from_latest_on_parent_doc_when_ref_empty(client, admin_user, project_with_doc):
    """Rung 3: ref with no sessions → falls through to parent doc's latest."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, doc_id)
    await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id, "model": "doc-model",
        "system_prompt_id": "doc-spid",
    })
    resp = await _post_session(client, token, {"project_id": pid, "reference_id": ref_id})
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == "doc-model"
    assert data["system_prompt_id"] == "doc-spid"


async def test_falls_back_to_project_defaults(client, admin_user, project_with_doc):
    """Rung 4: no parent_session, no scope history → model = CHAT_MODEL, prompts null/project-level."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await _post_session(client, token, {"project_id": pid, "document_id": doc_id})
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == CHAT_MODEL
    # system_prompt_id has no project-level fallback — must be null/empty.
    assert not data.get("system_prompt_id")


async def test_field_independence(client, admin_user, project_with_doc):
    """spec inv: each field walks the chain independently — first hit can be different ancestors."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _create_ref(client, token, pid, doc_id)
    # Ref has only `model`; doc has only `system_prompt_id`. Each must resolve to its first hit.
    await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id, "model": "doc-model",
        "system_prompt_id": "doc-spid",
    })
    await _post_session(client, token, {
        "project_id": pid, "reference_id": ref_id, "model": "ref-model",
    })
    resp = await _post_session(client, token, {"project_id": pid, "reference_id": ref_id})
    assert resp.status_code == 201
    data = resp.json()
    # model: rung 2 (ref-model from latest ref session)
    assert data["model"] == "ref-model"
    # system_prompt_id: ref has none; falls through to rung 3 (doc-spid)
    assert data["system_prompt_id"] == "doc-spid"
