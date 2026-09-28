"""Shared chat-test helpers: document + agent-session seeding.

# SYSTEM: chat-segments-tests — the ordered-`segments` suite this file once
# carried moved on; what remains are the seeding helpers other chat suites
# import (test_driver_client's F3 integration). The LLM-stream mock helpers
# that lived here died with the in-process loop, and the SSE completion
# helper died with the pump (plan agent-line-harness-lifecycle step 9).
"""


async def _create_doc(client, token, pid, *, content, title="Target"):
    resp = await client.post("/api/documents", json={"project_id": pid,
        "title": title, "content": content}, cookies={"lore_session": token})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


async def _agent_session(client, token, pid, *, target_doc_id, document_id):
    resp = await client.post("/api/chat/sessions", json={"project_id": pid,
        "document_id": document_id, "target_doc_id": target_doc_id,
        "model": "test"},
        cookies={"lore_session": token})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["session_id"]
