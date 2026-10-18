"""Chat search route — the person's own chats via `GET /api/projects/{id}/chat-search`.

The refused-principal cases run over BOTH entry points of the shared search core
(the agent's `search_my_chats` tool and this cookie route), so the one security
predicate cannot drift between them. Fixtures and the fake embedding space are
the tool suite's.
"""
import pytest
import test_chat_recall_tool as recall
from test_chat_recall_tool import _doc, _msg, _session

from jobs.tasks.chat_index import index_chat_messages

embed = recall.embed
world = recall.world
tree = recall.tree
skill_disabled = recall.skill_disabled

# The tool's anchor is the calling session's document; tree holds one anchor
# session per anchor document.
ANCHOR_SESSIONS = {"dA": "anchorA", "dA1": "anchorA1", "dB": "anchorB"}
ENTRIES = ["tool", "route"]


async def _call(client, world, entry, user, q, *, scope="project", under=None, anchor=None):
    """One search as `user` ('a' / 'b') through `entry`."""
    if entry == "tool":
        headers = {"Authorization": f"Bearer {world['k' + user]}"}
        if anchor:
            headers["X-Agent-Session-Id"] = ANCHOR_SESSIONS[anchor]
        body = {"query": q, **({"scope": scope} if scope != "project" else {}),
                **({"under_document_id": under} if under else {})}
        return await client.post("/api/tool/search_my_chats", json=body, headers=headers)
    params = {"q": q, "scope": scope, **({"under_document_id": under} if under else {}),
              **({"anchor_document_id": anchor} if anchor else {})}
    return await client.get(f"/api/projects/{world['pid']}/chat-search", params=params,
                            cookies={"lore_session": world["c" + user]})


async def _ids(client, world, entry, user, q, **kw) -> list[str]:
    resp = await _call(client, world, entry, user, q, **kw)
    assert resp.status_code == 200, resp.text
    return [h["message_id"] for h in resp.json()["hits"]]


# ─── the wall, over both entry points ────────────────────────────────────────


@pytest.mark.parametrize("entry", ENTRIES)
async def test_other_users_chat_never_returned(client, test_db, world, entry):
    pid = world["pid"]
    await _session(test_db, "b1", world["b"], pid)
    await _msg(test_db, "b1m0", "b1", "the dragon hoard is in the harbor")
    await index_chat_messages({}, world["b"], pid)
    assert await _ids(client, world, entry, "a", "dragon harbor") == []
    # the owner of the chat does find it — the index is not empty
    assert await _ids(client, world, entry, "b", "dragon harbor") == ["b1m0"]


@pytest.mark.parametrize("entry", ENTRIES)
async def test_chat_of_another_project_never_returned(client, test_db, world, entry):
    a = world["a"]
    await _session(test_db, "q1", a, "other-project")
    await _msg(test_db, "q1m0", "q1", "the comet returns to the orchard")
    await index_chat_messages({}, a, "other-project")
    assert await _ids(client, world, entry, "a", "comet orchard") == []


@pytest.mark.parametrize("entry", ENTRIES)
async def test_note_chats_never_returned(client, test_db, world, entry):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "n1", a, pid)
    await _msg(test_db, "n1m0", "n1", "a note about the ember glacier")
    await index_chat_messages({}, a, pid)
    await test_db.query("UPDATE type::record('chat_sessions', 'n1') SET is_note = true")
    assert await _ids(client, world, entry, "a", "ember glacier") == []


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("revoke", [
    "UPDATE type::record('messages', 'a1m0') SET deleted_at = time::now()",
    "UPDATE type::record('chat_sessions', 'a1') SET deleted_at = time::now()",
    "UPDATE type::record('chat_sessions', 'a1') SET unindexed = true",
    "UPDATE type::record('messages', 'a1m0') SET content = 'reworded entirely'",
], ids=["message-deleted", "session-deleted", "unindexed", "edited-away"])
async def test_revoked_rows_vanish_with_chunks_still_present(
        client, test_db, world, revoke, entry):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "a1", a, pid)
    await _msg(test_db, "a1m0", "a1", "the secret harbor ember")
    await index_chat_messages({}, a, pid)
    assert await _ids(client, world, entry, "a", "harbor ember") == ["a1m0"]
    await test_db.query(revoke)
    assert await test_db.query("SELECT id FROM chat_chunks WHERE message_id = 'a1m0'")
    assert await _ids(client, world, entry, "a", "harbor ember") == []


@pytest.mark.parametrize("entry", ENTRIES)
async def test_chat_of_a_cascade_deleted_document_vanishes(client, test_db, world, entry):
    from cascade import _cascade_delete_document

    from db import get_db

    pid, a = world["pid"], world["a"]
    await _doc(pid, "doomed-doc")
    await _session(test_db, "a1", a, pid, document_id="doomed-doc")
    await _msg(test_db, "a1m0", "a1", "the glacier orchard")
    await index_chat_messages({}, a, pid)
    assert await _ids(client, world, entry, "a", "glacier orchard") == ["a1m0"]
    await _cascade_delete_document(await get_db(), "doomed-doc")
    assert await _ids(client, world, entry, "a", "glacier orchard") == []


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("scope", ["project", "siblings", "subtree"])
async def test_other_users_and_unindexed_chats_stay_out_of_every_scope(
        client, test_db, tree, scope, entry):
    pid = tree["pid"]
    await _session(test_db, "bA", tree["b"], pid, document_id="dA")
    await _msg(test_db, "bAm", "bA", "the comet as b saw it")
    await index_chat_messages({}, tree["b"], pid)
    await test_db.query("UPDATE type::record('chat_sessions', 'sA1') SET unindexed = true")
    hits = await _ids(client, tree, entry, "a", "comet", scope=scope, anchor="dA",
                      under="dA" if scope == "subtree" else None)
    assert "bAm" not in hits and "sA1m" not in hits
    assert "sAm" in hits


@pytest.mark.parametrize("entry", ENTRIES)
async def test_disabled_skill_refuses(client, world, skill_disabled, entry):
    resp = await _call(client, world, entry, "a", "dragon")
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("entry", ENTRIES)
async def test_embeddings_unconfigured_is_503(client, world, embed, entry):
    import embeddings

    embed.side_effect = embeddings.EmbeddingConfigError("no url")
    resp = await _call(client, world, entry, "a", "dragon")
    assert resp.status_code == 503, resp.text


# ─── the route's own principal and arguments ─────────────────────────────────


@pytest.fixture
async def stranger(test_db):
    """A logged-in user who is not a member of the project."""
    from helpers import make_token

    from db import create_record

    uid = "test-stranger-001"
    await create_record("users", uid, {
        "name": "stranger", "email": "stranger@test.com", "password_hash": "x",
        "role": "user", "user_facts": "",
    })
    yield make_token(uid, "stranger", "user", "stranger@test.com")
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})


def _url(world) -> str:
    return f"/api/projects/{world['pid']}/chat-search"


async def test_api_key_bearer_is_401(client, world):
    resp = await client.get(_url(world), params={"q": "dragon"},
                            headers={"Authorization": f"Bearer {world['ka']}"})
    assert resp.status_code == 401, resp.text


async def test_non_member_is_403(client, world, stranger):
    resp = await client.get(_url(world), params={"q": "dragon"},
                            cookies={"lore_session": stranger})
    assert resp.status_code == 403, resp.text


async def test_anchor_marks_siblings_without_reordering(client, tree):
    without = (await _call(client, tree, "route", "a", "comet")).json()
    assert len(without["hits"]) == 6 and not any(h["sibling"] for h in without["hits"])
    resp = await _call(client, tree, "route", "a", "comet", anchor="dA1")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["query"] == "comet"
    # The fixture's hits tie on score, so "no reordering" is: the same hits, by score.
    assert sorted(h["message_id"] for h in result["hits"]) == sorted(
        h["message_id"] for h in without["hits"])
    scores = [h["score"] for h in result["hits"]]
    assert scores == sorted(scores, reverse=True)
    assert {h["chat_id"] for h in result["hits"] if h["sibling"]} == {"sA1", "sA1a"}


async def test_siblings_and_subtree_scopes(client, tree):
    assert sorted(await _ids(client, tree, "route", "a", "comet", scope="siblings",
                             anchor="dA")) == ["sA1am", "sA1m", "sAm", "sRm"]
    assert await _ids(client, tree, "route", "a", "comet", scope="subtree",
                      under="dB") == ["sBm"]


async def test_hits_carry_the_thread_and_its_document(client, test_db, tree):
    pid, a = tree["pid"], tree["a"]
    await _session(test_db, "r", a, pid, document_id="dB")
    await _session(test_db, "rb", a, pid, document_id="dB", thread_id="r")
    await _msg(test_db, "rbm", "rb", "the lighthouse in the branch")
    await index_chat_messages({}, a, pid)
    hit = (await _call(client, tree, "route", "a", "lighthouse")).json()["hits"][0]
    assert (hit["chat_id"], hit["thread_id"], hit["document_id"]) == ("rb", "r", "dB")


@pytest.mark.parametrize("anchor", ["no-such-doc", "dDeleted", "dForeign"])
async def test_anchor_unknown_deleted_or_foreign_is_404(client, test_db, tree, anchor):
    await _doc(tree["pid"], "dDeleted")
    await test_db.query("UPDATE type::record('documents', 'dDeleted') SET deleted_at = time::now()")
    await _doc("other-project", "dForeign")
    resp = await client.get(_url(tree), params={"q": "comet", "anchor_document_id": anchor},
                            cookies={"lore_session": tree["ca"]})
    assert resp.status_code == 404, resp.text


async def test_siblings_without_an_anchor_is_400(client, tree):
    resp = await _call(client, tree, "route", "a", "comet", scope="siblings")
    assert resp.status_code == 400, resp.text
    assert "no anchor document" in resp.json()["detail"]


@pytest.mark.parametrize("params", [
    {"q": "comet", "scope": "subtree"},
    {"q": "comet", "scope": "siblings", "under_document_id": "dA"},
    {"q": "comet", "under_document_id": "dA"},
    {"q": "comet", "scope": "everywhere"},
    {"q": ""},
], ids=["subtree-no-root", "siblings-with-root", "project-with-root", "bad-scope", "empty-q"])
async def test_argument_shape_is_422(client, tree, params):
    resp = await client.get(_url(tree), params=params, cookies={"lore_session": tree["ca"]})
    assert resp.status_code == 422, resp.text
