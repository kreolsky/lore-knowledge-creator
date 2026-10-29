"""chat-recall tool — `search_my_chats` over the caller's own other chats of the project.

Every visibility test drives the REFUSED principal: the chunks are present in the
index and the row that must not be served is still there, so only the read-time
predicate stands between it and the answer. Indexing runs through the real pair
task with the embedding provider faked at its boundary.
"""
import hashlib
import secrets

import pytest
from helpers import fake_embedding_provider

from jobs.tasks.chat_index import index_chat_messages

# Keyword axes of the fake embedding space; the last axis is a constant so no
# vector is ever all-zero (a text with no keyword scores ~0 against a query).
_AXES = ("dragon", "lighthouse", "comet", "harbor", "ember", "glacier", "orchard")


def _vec(text: str) -> list[float]:
    low = text.lower()
    return [1.0 if a in low else 0.0 for a in _AXES] + [0.01]


@pytest.fixture
def embed(monkeypatch):
    return fake_embedding_provider(monkeypatch, _vec)


@pytest.fixture
async def world(client, test_db, admin_user, regular_user, project_with_doc, embed):
    """Project P with members A (admin) and B; an agent key and a session cookie for each."""
    pid, _, a_uid = project_with_doc
    b_uid = regular_user[0]
    await test_db.query(
        "CREATE type::record('project_members', $id) SET project_id = $pid, "
        "user_id = $uid, access_level = 'full'",
        {"id": f"pm-{pid}-{b_uid}", "pid": pid, "uid": b_uid},
    )
    return {"pid": pid, "a": a_uid, "b": b_uid,
            "ka": await _key(a_uid, pid), "kb": await _key(b_uid, pid),
            "ca": admin_user[1], "cb": regular_user[1]}


async def _key(user_id: str, project_id: str, *, internal: bool = True) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"agent-key-{secrets.token_hex(4)}", {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent", "capabilities": ["agent"], "internal": internal,
    })
    return token


async def _session(db, sid, uid, pid, **fields):
    await db.query(
        "CREATE type::record('chat_sessions', $id) CONTENT $f",
        {"id": sid, "f": {"user_id": uid, "project_id": pid, "title": f"title {sid}",
                          "model": "m", **fields}},
    )


async def _msg(db, mid, chat_id, content, *, role="user", parent_id=None, **fields):
    await db.query(
        "CREATE type::record('messages', $id) CONTENT $f",
        {"id": mid, "f": {"chat_id": chat_id, "role": role, "content": content,
                          **({"parent_id": parent_id} if parent_id else {}), **fields}},
    )


async def _chain(db, chat_id, contents: list[str], prefix: str) -> list[str]:
    """A linear session: user/assistant alternating, each parented on the last."""
    ids, parent = [], None
    for i, text in enumerate(contents):
        mid = f"{prefix}{i}"
        await _msg(db, mid, chat_id, text, role=("user", "assistant")[i % 2],
                   parent_id=parent)
        ids.append(mid)
        parent = mid
    return ids


async def _call(client, token, body, session_id=None):
    headers = {"Authorization": f"Bearer {token}"}
    if session_id:
        headers["X-Agent-Session-Id"] = session_id
    return await client.post("/api/tool/search_my_chats", json=body, headers=headers)


async def _search(client, token, query, session_id=None) -> dict:
    resp = await _call(client, token, {"query": query}, session_id)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _hit_ids(result: dict) -> list[str]:
    return [h["message_id"] for h in result["hits"]]


# ─── the wall: who and where ─────────────────────────────────────────────────


async def test_own_other_chat_found_with_its_question(client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "a1", a, pid)
    await _chain(test_db, "a1", ["where does the dragon sleep?",
                                 "The dragon sleeps under the lighthouse."], "a1m")
    await index_chat_messages({}, a, pid)
    result = await _search(client, world["ka"], "dragon lighthouse")
    hit = next(h for h in result["hits"] if h["message_id"] == "a1m1")
    assert hit["chat_id"] == "a1" and hit["chat_title"] == "title a1"
    assert hit["role"] == "assistant"
    assert hit["question"] == "where does the dragon sleep?"
    assert "indexed" not in result


# ─── the current conversation ────────────────────────────────────────────────


async def test_original_and_branch_copies_collapse_to_one_hit(client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "o", a, pid)
    await _session(test_db, "o-b1", a, pid, thread_id="o")
    await _session(test_db, "o-b2", a, pid, thread_id="o")
    await _msg(test_db, "om", "o", "the lighthouse keeper")
    await _msg(test_db, "ob1m", "o-b1", "the lighthouse keeper", origin_id="om")
    await _msg(test_db, "ob2m", "o-b2", "the lighthouse keeper", origin_id="om")
    await index_chat_messages({}, a, pid)
    assert len((await _search(client, world["ka"], "lighthouse"))["hits"]) == 1


# ─── model guard + honest incompleteness ─────────────────────────────────────


async def test_model_change_drops_coverage_until_re_embedded(client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "a1", a, pid)
    await _msg(test_db, "a1m0", "a1", "ember dragon")
    await index_chat_messages({}, a, pid)
    assert "indexed" not in await _search(client, world["ka"], "dragon")
    # The deployed EMBEDDING_MODEL moved on: every stored chunk is now another model's.
    await test_db.query("UPDATE chat_chunks SET model = 'previous-model'")
    result = await _search(client, world["ka"], "dragon")
    assert (result["hits"], result["indexed"]) == ([], "0 of 1")
    await index_chat_messages({}, a, pid)
    result = await _search(client, world["ka"], "dragon")
    assert _hit_ids(result) == ["a1m0"] and "indexed" not in result


# ─── the admin off-switch ────────────────────────────────────────────────────


@pytest.fixture
async def skill_disabled(test_db):
    """The admin's off-switch; removed after, since the shared cleanup keeps instance_skills."""
    await test_db.query(
        "CREATE instance_skills CONTENT {name: 'chat-recall', enabled: false, updated_by: 'admin'}")
    yield
    await test_db.query("DELETE instance_skills WHERE name = 'chat-recall'")


async def test_disabled_skill_refuses(client, world, skill_disabled):
    resp = await _call(client, world["ka"], {"query": "dragon"})
    assert resp.status_code == 403, resp.text
    resp = await _call(client, world["ka"], {"message_id": "whatever"})
    assert resp.status_code == 403, resp.text


# ─── scope: siblings / subtree / sibling priority ────────────────────────────


async def _doc(pid, did, parent=None, **fields):
    from db import create_record

    await create_record("documents", did, {
        "project_id": pid, "parent_id": parent, "title": did, "content": "",
        "path": f"{did}.md", "is_index": False, **fields,
    })


@pytest.fixture
async def tree(test_db, world):
    """Documents A, A/A1, A/A1/A1a, A/R (a reference), B (A's sibling); A's own
    chats on each plus a project-level one, all on one topic; one anchor
    session (no messages) per anchor document."""
    pid, a = world["pid"], world["a"]
    await _doc(pid, "dA")
    await _doc(pid, "dA1", "dA")
    await _doc(pid, "dA1a", "dA1")
    await _doc(pid, "dR", "dA", is_reference=True)
    await _doc(pid, "dB")
    for sid, did in (("sA", "dA"), ("sA1", "dA1"), ("sA1a", "dA1a"), ("sR", "dR"),
                     ("sB", "dB"), ("sP", None)):
        await _session(test_db, sid, a, pid, **({"document_id": did} if did else {}))
        await _msg(test_db, f"{sid}m", sid, f"the comet over {sid}")
    for sid, did in (("anchorA", "dA"), ("anchorA1", "dA1"), ("anchorB", "dB"),
                     ("anchorP", None)):
        await _session(test_db, sid, a, pid, **({"document_id": did} if did else {}))
    await index_chat_messages({}, a, pid)
    return world


@pytest.mark.parametrize("header", [None, "anchorP", "b-anchor"],
                         ids=["no-header", "project-level", "foreign"])
async def test_siblings_without_an_anchor_is_400(client, test_db, tree, header):
    await _session(test_db, "b-anchor", tree["b"], tree["pid"], document_id="dA")
    resp = await _call(client, tree["ka"], {"query": "comet", "scope": "siblings"}, header)
    assert resp.status_code == 400, resp.text
    assert "no anchor document" in resp.json()["detail"]


# ─── window read ─────────────────────────────────────────────────────────────


async def _long_chat(db, a, pid) -> list[str]:
    await _session(db, "w", a, pid)
    return await _chain(db, "w", [f"turn {i // 2} {('q', 'a')[i % 2]}" for i in range(10)],
                        "w")


async def _window(client, token, message_id, offset=0, session_id=None):
    resp = await _call(client, token, {"message_id": message_id, "offset": offset},
                       session_id)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(window: dict) -> list[str]:
    return [m["message_id"] for m in window["messages"]]


async def test_window_skips_deleted_rows_but_keeps_the_chain(client, test_db, world):
    await _long_chat(test_db, world["a"], world["pid"])
    await test_db.query("UPDATE type::record('messages', 'w3') SET deleted_at = time::now()")
    win = await _window(client, world["ka"], "w4")
    assert _ids(win) == ["w2", "w4", "w5", "w6", "w7"]


async def test_window_pages_a_message_over_budget(client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "big", a, pid)
    body = "x" * 9000 + "TAIL"
    await _chain(test_db, "big", ["short question", body, "next q", "next a"], "big")
    first = await _window(client, world["ka"], "big1")
    served = sum(len(m["content"]) for m in first["messages"])
    assert served <= 8000
    assert first["next_offset"] == 8000
    rest = await _window(client, world["ka"], "big1", offset=first["next_offset"])
    hit = next(m for m in rest["messages"] if m["message_id"] == "big1")
    assert hit["content"].endswith("TAIL") and "next_offset" not in rest


# ─── surface + skill ─────────────────────────────────────────────────────────


async def test_external_key_is_refused_search_is_in_app_only(client, test_db, world):
    """Operator ruling 2026-10-24: the chat-recall pack is closed to EXTERNAL
    keys — the person's chat corpus is the in-app assistant's own memory."""
    ext = await _key(world["a"], world["pid"], internal=False)
    resp = await _call(client, ext, {"query": "anything"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "chat-recall is available only to the in-app assistant"
