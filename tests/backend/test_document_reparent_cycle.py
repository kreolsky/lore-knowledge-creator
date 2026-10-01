"""Re-parent via PATCH /api/documents/{id} runs the ONE in-project move.

Cycles (self, descendant) are refused, and the move's node rules apply on this
surface too: a system document is unmovable, a reference keeps no sort_key and
cannot sit at the project root. The Memory subtree is not a destination for a
document that is not already memory; moves inside it stay legal.
"""

import pytest
import pytest_asyncio

from db import extract_id


async def _mk_doc(client, pid, token, *, title, parent_id=None):
    r = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title, "parent_id": parent_id},
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


async def _reparent(client, token, doc_id, parent_id):
    return await client.patch(
        f"/api/documents/{doc_id}",
        json={"parent_id": parent_id},
        cookies={"lore_session": token},
    )


async def _parent_of(test_db, doc_id):
    rows = await test_db.query(
        "SELECT parent_id FROM type::record('documents', $id)", {"id": doc_id},
    )
    return extract_id(rows[0]["parent_id"])


@pytest_asyncio.fixture
async def chain(client, admin_user, project_with_doc):
    """A → B → C under the index doc, plus a sibling S of A."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    a = await _mk_doc(client, pid, token, title="A", parent_id=idx_id)
    b = await _mk_doc(client, pid, token, title="B", parent_id=a)
    c = await _mk_doc(client, pid, token, title="C", parent_id=b)
    s = await _mk_doc(client, pid, token, title="S", parent_id=idx_id)
    return token, idx_id, a, c, s


@pytest.mark.asyncio
async def test_reparent_to_self_is_refused(client, test_db, chain):
    token, idx_id, a, _, _ = chain
    resp = await _reparent(client, token, a, a)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "A document cannot be its own parent"
    assert await _parent_of(test_db, a) == idx_id


@pytest.mark.asyncio
async def test_reparent_into_grandchild_is_refused(client, test_db, chain):
    token, idx_id, a, c, _ = chain
    resp = await _reparent(client, token, a, c)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Cannot move a document into its own subtree (cycle)"
    assert await _parent_of(test_db, a) == idx_id


@pytest.mark.asyncio
async def test_reparent_under_sibling_is_accepted(client, test_db, chain):
    token, _, a, _, s = chain
    resp = await _reparent(client, token, a, s)
    assert resp.status_code == 200, resp.text
    assert await _parent_of(test_db, a) == s


async def _mk_ref(client, pid, token, *, host):
    r = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Ref", "content": "r",
              "is_reference": True, "media_type": "markdown", "parent_id": host},
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


@pytest.mark.asyncio
async def test_reparent_system_document_is_refused(client, test_db, chain):
    token, idx_id, a, _, s = chain
    await test_db.query(
        "UPDATE type::record('documents', $id) SET is_system = true", {"id": a},
    )
    resp = await _reparent(client, token, a, s)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "System documents cannot be moved"
    assert await _parent_of(test_db, a) == idx_id


@pytest.mark.asyncio
async def test_reparent_reference_to_project_level_is_refused(
    client, test_db, chain, project_with_doc,
):
    token, idx_id, a, _, _ = chain
    pid, _, _ = project_with_doc
    ref = await _mk_ref(client, pid, token, host=a)
    resp = await _reparent(client, token, ref, "")
    assert resp.status_code == 400, resp.text
    assert "index document" in resp.json()["detail"]
    assert await _parent_of(test_db, ref) == a


@pytest.mark.asyncio
async def test_reparent_reference_gets_top_key_of_new_host_group(
    client, test_db, chain, project_with_doc,
):
    token, _, a, _, s = chain
    pid, _, _ = project_with_doc
    existing = await _mk_ref(client, pid, token, host=s)
    ref = await _mk_ref(client, pid, token, host=a)
    resp = await _reparent(client, token, ref, s)
    assert resp.status_code == 200, resp.text
    rows = await test_db.query(
        "SELECT parent_id, sort_key, is_reference FROM type::record('documents', $id)",
        {"id": ref},
    )
    assert extract_id(rows[0]["parent_id"]) == s
    assert rows[0].get("sort_key"), "a re-hosted ref carries the new group's top key"
    assert rows[0]["is_reference"] is True
    existing_key = (await test_db.query(
        "SELECT sort_key FROM type::record('documents', $id)", {"id": existing},
    ))[0]["sort_key"]
    assert rows[0]["sort_key"] < existing_key, "re-host lands at the TOP of the group"


@pytest_asyncio.fixture
async def memory(client, admin_user, project_with_doc):
    """The seeded Memory folder with one card under it."""
    from agent_config import ensure_agent_system_docs

    pid, _, _ = project_with_doc
    _, token = admin_user
    folder = (await ensure_agent_system_docs(pid))["memory_folder"]
    card = await _mk_doc(client, pid, token, title="Card", parent_id=folder)
    return folder, card


@pytest.mark.asyncio
async def test_reparent_under_memory_folder_is_refused(client, test_db, chain, memory):
    token, idx_id, a, _, _ = chain
    folder, _ = memory
    resp = await _reparent(client, token, a, folder)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "A document cannot be moved into the Memory folder"
    assert await _parent_of(test_db, a) == idx_id


@pytest.mark.asyncio
async def test_reparent_under_memory_card_is_refused(client, test_db, chain, memory):
    token, idx_id, a, _, _ = chain
    _, card = memory
    resp = await _reparent(client, token, a, card)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "A document cannot be moved into the Memory folder"
    assert await _parent_of(test_db, a) == idx_id


@pytest.mark.asyncio
async def test_memory_card_moves_inside_memory(
    client, test_db, admin_user, project_with_doc, memory,
):
    pid, _, _ = project_with_doc
    _, token = admin_user
    folder, card = memory
    other = await _mk_doc(client, pid, token, title="Card 2", parent_id=folder)
    resp = await _reparent(client, token, card, other)
    assert resp.status_code == 200, resp.text
    assert await _parent_of(test_db, card) == other
