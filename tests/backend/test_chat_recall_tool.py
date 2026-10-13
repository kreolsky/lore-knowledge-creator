"""chat-recall tool — `search_my_chats` over the caller's own other chats of the project.

Every visibility test drives the REFUSED principal: the chunks are present in the
index and the row that must not be served is still there, so only the read-time
predicate stands between it and the answer. Indexing runs through the real pair
task with the embedding provider faked at its boundary.
"""
import hashlib
import secrets
from datetime import datetime, timezone

import agent_skills
import pytest
from agent_tools.registry import by_name, mcp_entries
from helpers import fake_embedding_provider, skill_frontmatter

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


async def _key(user_id: str, project_id: str) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"agent-key-{secrets.token_hex(4)}", {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent", "capabilities": ["agent"],
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


async def _conversation(db, a, pid):
    """root c0, its branch c1, a compaction continuation c2 of c1, and an
    unrelated chat x — each holding the same keyword."""
    await _session(db, "c0", a, pid)
    await _session(db, "c1", a, pid, thread_id="c0")
    await _session(db, "c2", a, pid, compacted_from="c1")
    await _session(db, "x", a, pid)
    for sid in ("c0", "c1", "c2", "x"):
        await _msg(db, f"{sid}m", sid, f"comet seen from {sid}")
    await index_chat_messages({}, a, pid)


async def test_whole_current_conversation_is_excluded(client, test_db, world):
    await _conversation(test_db, world["a"], world["pid"])
    assert _hit_ids(await _search(client, world["ka"], "comet", session_id="c2")) == ["xm"]


@pytest.mark.parametrize("header", [None, "b-session"], ids=["absent", "foreign"])
async def test_no_or_foreign_session_means_no_exclusion(client, test_db, world, header):
    pid = world["pid"]
    await _conversation(test_db, world["a"], pid)
    await _session(test_db, "b-session", world["b"], pid)
    await _msg(test_db, "bm", "b-session", "comet seen by b")
    await index_chat_messages({}, world["b"], pid)
    hits = _hit_ids(await _search(client, world["ka"], "comet", session_id=header))
    assert sorted(hits) == ["c0m", "c1m", "c2m", "xm"]


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


async def test_chunks_of_another_model_or_dimension_are_not_scored(client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "a1", a, pid)
    await _msg(test_db, "a1m0", "a1", "dragon in the orchard")
    await test_db.query("INSERT INTO chat_chunks $rows", {"rows": [{
        "user_id": a, "project_id": pid, "chat_id": "a1", "message_id": "a1m0",
        "origin_key": "a1m0", "role": "user", "created_at": datetime.now(timezone.utc),
        "ord": 0, "content": "dragon in the orchard", "model": model,
        "content_hash": hashlib.sha256(b"dragon in the orchard").hexdigest(),
        "embedding": vec,
    } for model, vec in (("old-model", _vec("dragon orchard")), ("old-model", [1.0] * 3))]})
    result = await _search(client, world["ka"], "dragon orchard")
    assert result["hits"] == []
    assert result["indexed"] == "0 of 1"


async def test_pending_messages_are_reported(client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "a1", a, pid)
    await _msg(test_db, "a1m0", "a1", "indexed dragon")
    await index_chat_messages({}, a, pid)
    await _msg(test_db, "a1m1", "a1", "pending dragon", role="assistant")
    result = await _search(client, world["ka"], "dragon")
    assert _hit_ids(result) == ["a1m0"]
    assert result["indexed"] == "1 of 2"


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


async def test_query_is_embedded_with_the_retrieval_instruction(client, test_db, world, embed):
    import settings

    await _search(client, world["ka"], "anything")
    assert embed.await_args.kwargs["instruction"] == await settings.get(
        "RETRIEVAL_QUERY_INSTRUCTION")


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


@pytest.mark.parametrize("body", [{}, {"query": "x", "message_id": "y"}],
                         ids=["neither", "both"])
async def test_exactly_one_mode(client, world, body):
    assert (await _call(client, world["ka"], body)).status_code == 422


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


async def _scoped(client, token, body, session_id=None, status=200) -> dict:
    resp = await _call(client, token, body, session_id)
    assert resp.status_code == status, resp.text
    return resp.json()


async def test_siblings_are_the_anchor_and_its_subtree(client, tree):
    result = await _scoped(client, tree["ka"], {"query": "comet", "scope": "siblings"},
                           "anchorA")
    assert sorted(_hit_ids(result)) == ["sA1am", "sA1m", "sAm", "sRm"]
    assert all(h["sibling"] is True for h in result["hits"])
    by_chat = {h["chat_id"]: h for h in result["hits"]}
    assert by_chat["sA1"]["document_id"] == "dA1" and by_chat["sA1"]["thread_id"] == "sA1"


async def test_siblings_from_a_child_anchor_exclude_its_parent(client, tree):
    result = await _scoped(client, tree["ka"], {"query": "comet", "scope": "siblings"},
                           "anchorA1")
    assert sorted(_hit_ids(result)) == ["sA1am", "sA1m"]


async def test_subtree_under_a_document(client, tree):
    result = await _scoped(client, tree["ka"],
                           {"query": "comet", "scope": "subtree", "under_document_id": "dB"})
    assert _hit_ids(result) == ["sBm"]


@pytest.mark.parametrize("root", ["no-such-doc", "dDeleted", "dForeign"])
async def test_subtree_root_unknown_deleted_or_foreign_is_404(client, test_db, tree, root):
    await _doc(tree["pid"], "dDeleted")
    await test_db.query("UPDATE type::record('documents', 'dDeleted') SET deleted_at = time::now()")
    await _doc("other-project", "dForeign")
    await _scoped(client, tree["ka"],
                  {"query": "comet", "scope": "subtree", "under_document_id": root},
                  status=404)


@pytest.mark.parametrize("body", [
    {"query": "comet", "scope": "subtree"},
    {"query": "comet", "scope": "siblings", "under_document_id": "dA"},
    {"query": "comet", "under_document_id": "dA"},
    {"message_id": "sAm", "scope": "project"},
    {"message_id": "sAm", "scope": "siblings"},
    {"message_id": "sAm", "under_document_id": "dA"},
], ids=["subtree-no-root", "siblings-with-root", "project-with-root",
        "window-explicit-project", "window-siblings", "window-root"])
async def test_scope_argument_shape_is_422(client, tree, body):
    assert (await _call(client, tree["ka"], body)).status_code == 422


async def test_window_read_alone_still_works_with_scope_fields_absent(client, tree):
    win = await _window(client, tree["ka"], "sBm")
    assert win["chat_id"] == "sB"


@pytest.mark.parametrize("header", [None, "anchorP", "b-anchor"],
                         ids=["no-header", "project-level", "foreign"])
async def test_siblings_without_an_anchor_is_400(client, test_db, tree, header):
    await _session(test_db, "b-anchor", tree["b"], tree["pid"], document_id="dA")
    resp = await _call(client, tree["ka"], {"query": "comet", "scope": "siblings"}, header)
    assert resp.status_code == 400, resp.text
    assert "no anchor document" in resp.json()["detail"]


async def test_project_scope_without_anchor_has_no_sibling_tier(client, tree):
    result = await _scoped(client, tree["ka"], {"query": "comet"})
    assert len(result["hits"]) == 6
    assert not any(h["sibling"] for h in result["hits"])
    assert next(h for h in result["hits"] if h["chat_id"] == "sP")["document_id"] is None


async def test_reparented_root_carries_its_branches(client, test_db, admin_user, tree):
    pid, a = tree["pid"], tree["a"]
    await _session(test_db, "r", a, pid, document_id="dA")
    await _session(test_db, "rb", a, pid, document_id="dA", thread_id="r")
    await _msg(test_db, "rm", "r", "the glacier at the root")
    await _msg(test_db, "rbm", "rb", "the glacier in the branch")
    await index_chat_messages({}, a, pid)
    resp = await client.patch("/api/chat/sessions/r", json={"document_id": "dB"},
                              cookies={"lore_session": admin_user[1]})
    assert resp.status_code == 200, resp.text
    from_a = await _scoped(client, tree["ka"], {"query": "glacier", "scope": "siblings"},
                           "anchorA")
    assert _hit_ids(from_a) == []
    under_b = await _scoped(client, tree["ka"],
                            {"query": "glacier", "scope": "subtree", "under_document_id": "dB"})
    assert sorted(_hit_ids(under_b)) == ["rbm", "rm"]
    assert {h["thread_id"] for h in under_b["hits"]} == {"r"}
    assert {h["document_id"] for h in under_b["hits"]} == {"dB"}


async def _pool(db, a, pid, prefix, count, doc, text):
    for i in range(count):
        sid = f"{prefix}{i}"
        await _session(db, sid, a, pid, **({"document_id": doc} if doc else {}))
        await _msg(db, f"{sid}m", sid, text)


async def test_lower_scoring_sibling_is_ranked_first(client, test_db, tree):
    pid, a = tree["pid"], tree["a"]
    await _pool(test_db, a, pid, "far", 8, "dB", "dragon by the lighthouse")
    await _pool(test_db, a, pid, "near", 1, "dA1", "a dragon")
    await index_chat_messages({}, a, pid)
    result = await _scoped(client, tree["ka"], {"query": "dragon lighthouse"}, "anchorA")
    hits = result["hits"]
    assert len(hits) == 8
    assert hits[0]["message_id"] == "near0m" and hits[0]["sibling"] is True
    assert hits[0]["score"] < hits[1]["score"]
    assert not any(h["sibling"] for h in hits[1:])


async def test_sibling_slots_are_capped_and_the_rest_fill_from_others(client, test_db, tree):
    pid, a = tree["pid"], tree["a"]
    await _pool(test_db, a, pid, "far", 8, "dB", "ember harbor")
    await _pool(test_db, a, pid, "near", 8, "dA1", "ember harbor")
    await index_chat_messages({}, a, pid)
    hits = (await _scoped(client, tree["ka"], {"query": "ember harbor"}, "anchorA"))["hits"]
    assert [h["sibling"] for h in hits] == [True] * 5 + [False] * 3


async def test_no_sibling_hits_gives_every_slot_to_others(client, test_db, tree):
    pid, a = tree["pid"], tree["a"]
    await _pool(test_db, a, pid, "far", 9, "dB", "orchard glacier")
    await index_chat_messages({}, a, pid)
    hits = (await _scoped(client, tree["ka"], {"query": "orchard glacier"}, "anchorA"))["hits"]
    assert len(hits) == 8 and not any(h["sibling"] for h in hits)


async def test_one_origin_across_both_pools_is_one_sibling_hit(client, test_db, tree):
    pid, a = tree["pid"], tree["a"]
    await _session(test_db, "orig", a, pid, document_id="dA1")
    await _session(test_db, "cont", a, pid, compacted_from="orig")
    await _msg(test_db, "origm", "orig", "harbor lighthouse")
    await _msg(test_db, "contm", "cont", "harbor lighthouse", origin_id="origm")
    await index_chat_messages({}, a, pid)
    hits = (await _scoped(client, tree["ka"], {"query": "harbor lighthouse"}, "anchorA"))["hits"]
    assert len(hits) == 1 and hits[0]["sibling"] is True


async def test_coverage_counts_only_the_searched_sessions(client, test_db, tree):
    await _msg(test_db, "sBpending", "sB", "a pending comet", role="assistant")
    narrow = await _scoped(client, tree["ka"], {"query": "comet", "scope": "siblings"},
                           "anchorA")
    assert "indexed" not in narrow
    wide = await _scoped(client, tree["ka"], {"query": "comet"}, "anchorA")
    assert wide["indexed"] == "6 of 7"


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


async def test_window_is_hit_turn_plus_one_each_side(client, test_db, world):
    await _long_chat(test_db, world["a"], world["pid"])
    win = await _window(client, world["ka"], "w5")  # turn 2's assistant reply
    assert _ids(win) == ["w2", "w3", "w4", "w5", "w6", "w7"]
    assert (win["earlier"], win["later"]) == ("w0", "w8")
    assert win["chat_id"] == "w" and "next_offset" not in win
    edge = await _window(client, world["ka"], "w0")
    assert _ids(edge) == ["w0", "w1", "w2", "w3"]
    assert edge.get("earlier") is None and edge["later"] == "w4"


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


@pytest.mark.parametrize("who", ["other-user", "current-conversation"])
async def test_window_obeys_the_same_predicate(client, test_db, world, who):
    pid = world["pid"]
    await _session(test_db, "b1", world["b"], pid)
    await _msg(test_db, "b1m0", "b1", "b's private words")
    await _session(test_db, "a1", world["a"], pid)
    await _msg(test_db, "a1m0", "a1", "a's own words")
    target, session = ("b1m0", None) if who == "other-user" else ("a1m0", "a1")
    resp = await _call(client, world["ka"], {"message_id": target}, session)
    assert resp.status_code == 404, resp.text


# ─── surface + skill ─────────────────────────────────────────────────────────


def test_tool_is_absent_from_the_mcp_surface():
    from mcp_gateway.schemas import build_tool_list

    entry = by_name("search_my_chats")
    assert entry.surfaces == {"agent", "tool_api"}
    assert entry not in mcp_entries()
    assert "search_my_chats" not in {t.name for t in build_tool_list()}


async def test_skill_pack_is_served():
    from agent.tools import agent_toolset

    content = (agent_skills.CONFIGS_DIR / "skill_chat_recall.md").read_text(encoding="utf-8")
    skill = skill_frontmatter(content.strip())
    assert skill is not None and skill["name"] == "chat-recall"
    served = {t["function"]["name"] for t in await agent_toolset()}
    assert skill["tools"] == ["search_my_chats"]
    assert set(skill["tools"]) <= served
