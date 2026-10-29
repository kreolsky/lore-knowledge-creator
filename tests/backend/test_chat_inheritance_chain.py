"""Tests for the 4-rung inheritance chain (Spec §4.2).

Each new chat resolves missing fields (model, system_prompt_id)
via: parent_session → latest on same scope → latest on parent doc (for refs) → project defaults.
Each field walks the chain INDEPENDENTLY — first hit per field.

# Plan "unify-agent-config": `system_prompts_doc_id` removed from the chain.
"""


def _post_session(client, token, body):
    return client.post("/api/chat/sessions", json=body, cookies={"lore_session": token})


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
