"""GET /api/chat/sessions/{id}/last-message-preview — lazy hover-preview source
for the chat list plaque. Returns the newest non-deleted message content,
truncated in SQL (never transfers full bodies / image data URLs)."""

import pytest


async def _create_session(client, token, pid, doc_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["session_id"]


async def _add_message(client, token, sid, content, role="user"):
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"role": role, "content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["message_id"]


@pytest.mark.asyncio
async def test_returns_last_message_content(client, project_with_doc):
    pid, doc_id, _ = project_with_doc
    token = _token(project_with_doc)
    sid = await _create_session(client, token, pid, doc_id)
    await _add_message(client, token, sid, "first")
    await _add_message(client, token, sid, "second (newest)")

    resp = await client.get(
        f"/api/chat/sessions/{sid}/last-message-preview",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["preview"] == "second (newest)"


@pytest.mark.asyncio
async def test_truncates_long_content(client, project_with_doc):
    pid, doc_id, _ = project_with_doc
    token = _token(project_with_doc)
    sid = await _create_session(client, token, pid, doc_id)
    await _add_message(client, token, sid, "x" * 3000)

    resp = await client.get(
        f"/api/chat/sessions/{sid}/last-message-preview",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["preview"]) == 2000


@pytest.mark.asyncio
async def test_forbidden_without_access(client, project_with_doc, regular_user):
    pid, doc_id, _ = project_with_doc
    owner_token = _token(project_with_doc)
    sid = await _create_session(client, owner_token, pid, doc_id)
    await _add_message(client, owner_token, sid, "secret")

    _, other_token = regular_user
    resp = await client.get(
        f"/api/chat/sessions/{sid}/last-message-preview",
        cookies={"lore_session": other_token},
    )
    assert resp.status_code in (403, 404), resp.text


def _token(project_with_doc):
    # project_with_doc's owner is admin_user; reuse its token via the app's
    # make_token helper is unavailable here, so mint from the known admin uid.
    from helpers import make_token
    return make_token("test-admin-001", "testadmin", "admin", "admin@test.com")
