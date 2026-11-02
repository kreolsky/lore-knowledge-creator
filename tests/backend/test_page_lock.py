"""Page PIN lock (SYSTEM: page-lock, plan page-pin-lock step 1) — server side.

The lock is PERSONAL: users.locked_doc_ids lives on the LOCKING user, not on
the doc — other members see the page as before. The agent acting for the
locking user must not read or find the locked page; the cabinet endpoints
guard the set with the ACCOUNT PIN (no new secret, no new rate limiter).

Two tests, within the diet's 3-per-commit ceiling; the rest of the surface —
/me field, 422 without a PIN, personal semantics, export refusal,
build_context drop — is proven by the step-2 live drive.
"""

import hashlib
import secrets

import pytest


async def _make_agent_key(test_db, user_id: str, project_id: str) -> str:
    """Insert a project-scoped agent API key and return the plaintext token."""
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"agent-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": "",  # project-scoped
        "token_hash": token_hash,
        "label": "page-lock-test",
        "capabilities": ["agent"],
    })
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_doc(client, token: str, project_id: str, title: str, content: str = "") -> str:
    resp = await client.post(
        "/api/documents",
        json={"project_id": project_id, "title": title, "content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _set_pin(client, token: str, pin: str = "4321") -> None:
    resp = await client.patch(
        "/api/cabinet/pin", json={"pin": pin}, cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text


async def _lock(client, token: str, doc_id: str) -> list[str]:
    resp = await client.post(
        f"/api/cabinet/locked-docs/{doc_id}", cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["locked_doc_ids"]


async def _agent_read(client, agent_tok: str, doc_id: str):
    return await client.post(
        "/api/tool/read_document", json={"document_id": doc_id},
        headers=_hdr(agent_tok),
    )


async def _agent_search(client, agent_tok: str, marker: str):
    return await client.post(
        "/api/tool/search_materials",
        json={"query": marker, "mode": "exact", "k": 10},
        headers=_hdr(agent_tok),
    )


# TEST-WHY: refused-principal — the locking owner's agent is refused at BOTH agent choke points: read_document gets a 403 naming the lock (never 404-wording — scope.py INVARIANT), and search_materials drops the page from hits entirely (a snippet leak is a body leak).
@pytest.mark.asyncio
async def test_agent_read_and_search_refuse_locked_page(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    uid, token = admin_user
    await _set_pin(client, token)
    marker = "zxqk-lockup-marker-7717"
    doc_id = await _make_doc(client, token, pid, "DiaryLock", f"personal diary {marker}")
    agent_tok = await _make_agent_key(test_db, uid, pid)

    # Before the lock: readable and findable.
    rd = await _agent_read(client, agent_tok, doc_id)
    assert rd.status_code == 200
    sr = await _agent_search(client, agent_tok, marker)
    assert sr.status_code == 200, sr.text
    assert doc_id in [h["doc_id"] for h in sr.json()["hits"]]

    locked = await _lock(client, token, doc_id)
    assert locked == [doc_id]

    # After the lock: refused (403, lock-named detail — not a 404 wording) and
    # invisible to search.
    rd = await _agent_read(client, agent_tok, doc_id)
    assert rd.status_code == 403
    assert rd.json()["detail"] == "This page is locked by the user"
    sr = await _agent_search(client, agent_tok, marker)
    assert sr.status_code == 200, sr.text
    assert doc_id not in [h["doc_id"] for h in sr.json()["hits"]]


# TEST-WHY: refused-principal — removing a lock re-verifies the account PIN (same secret, same rate limiter): a wrong PIN is a 403, never a silent unlock; the right PIN empties the set and the agent reads again.
@pytest.mark.asyncio
async def test_remove_lock_wrong_pin_refused(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    uid, token = admin_user
    await _set_pin(client, token)
    doc_id = await _make_doc(client, token, pid, "UnlockDiary", "secret text")
    await _lock(client, token, doc_id)
    agent_tok = await _make_agent_key(test_db, uid, pid)

    wrong = await client.post(
        f"/api/cabinet/locked-docs/{doc_id}/remove",
        json={"pin": "0000"}, cookies={"lore_session": token},
    )
    assert wrong.status_code == 403
    assert wrong.json()["detail"] == "Wrong PIN"
    rd = await _agent_read(client, agent_tok, doc_id)
    assert rd.status_code == 403, "a wrong PIN must not unlock"

    right = await client.post(
        f"/api/cabinet/locked-docs/{doc_id}/remove",
        json={"pin": "4321"}, cookies={"lore_session": token},
    )
    assert right.status_code == 200, right.text
    assert right.json()["locked_doc_ids"] == []

    rd = await _agent_read(client, agent_tok, doc_id)
    assert rd.status_code == 200
    assert "secret text" in rd.json()["content"]
