"""chat-recall tools — `list_my_chats` + `read_my_chat` (plan chat-list-and-read).

Every visibility test drives the REFUSED principal: the row that must not be
served is present in the DB, so only the read-time predicate (`visible_sessions`)
stands between it and the answer. Neither tool touches the embedding index, so
no indexing runs here.
"""
import hashlib
import secrets
from datetime import datetime, timezone

import pytest
from agent_tools.registry import by_name, mcp_entries

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
T2 = datetime(2026, 1, 3, tzinfo=timezone.utc)
T3 = datetime(2026, 1, 4, tzinfo=timezone.utc)


def _at(t: datetime) -> dict:
    return {"created_at": t}


@pytest.fixture
async def world(client, test_db, admin_user, regular_user, project_with_doc):
    """Project P with members A (admin) and B; an agent key for each."""
    pid, _, a_uid = project_with_doc
    b_uid = regular_user[0]
    await test_db.query(
        "CREATE type::record('project_members', $id) SET project_id = $pid, "
        "user_id = $uid, access_level = 'full'",
        {"id": f"pm-{pid}-{b_uid}", "pid": pid, "uid": b_uid},
    )
    return {"pid": pid, "a": a_uid, "b": b_uid,
            "ka": await _key(a_uid, pid), "kb": await _key(b_uid, pid)}


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


async def _doc(pid, did, parent=None, **fields):
    from db import create_record

    await create_record("documents", did, {
        "project_id": pid, "parent_id": parent, "title": f"doc {did}", "content": "",
        "path": f"{did}.md", "is_index": False, **fields,
    })


async def _post(client, token, path, body, session_id=None, status=200) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if session_id:
        headers["X-Agent-Session-Id"] = session_id
    resp = await client.post(f"/api/tool/{path}", json=body, headers=headers)
    assert resp.status_code == status, resp.text
    return resp.json()


async def _list(client, token, body=None, session_id=None, status=200) -> dict:
    return await _post(client, token, "list_my_chats", body or {}, session_id, status)


async def _read(client, token, chat_id, from_turn=0, session_id=None, status=200) -> dict:
    return await _post(client, token, "read_my_chat",
                       {"chat_id": chat_id, "from_turn": from_turn}, session_id, status)


def _chat_ids(result: dict) -> list[str]:
    return [r["chat_id"] for r in result["rows"]]


# ─── list: the wall — who and where ──────────────────────────────────────────


async def test_lists_only_the_callers_own_live_chats(client, test_db, world):
    pid, a, b = world["pid"], world["a"], world["b"]
    await _session(test_db, "mine", a, pid, title="diary", **_at(T0))
    await _msg(test_db, "minem", "mine", "hello", **_at(T1))
    await _session(test_db, "bchat", b, pid, title="b's diary")
    await _msg(test_db, "bchatm", "bchat", "private words")
    await _session(test_db, "otherproj", a, "other-project")
    await _msg(test_db, "otherprojm", "otherproj", "elsewhere")
    await _session(test_db, "gone", a, pid)
    await test_db.query(
        "UPDATE type::record('chat_sessions', 'gone') SET deleted_at = time::now()")
    await _session(test_db, "anote", a, pid, is_note=True)
    await _session(test_db, "hidden", a, pid)
    await test_db.query(
        "UPDATE type::record('chat_sessions', 'hidden') SET unindexed = true")
    result = await _list(client, world["ka"])
    assert _chat_ids(result) == ["mine"]
    row = result["rows"][0]
    assert (row["title"], row["message_count"]) == ("diary", 1)
    assert row["thread_id"] == "mine" and row["document_id"] is None


async def test_the_whole_current_conversation_is_excluded(client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "c0", a, pid)
    await _session(test_db, "c1", a, pid, thread_id="c0")
    await _session(test_db, "c2", a, pid, compacted_from="c1")
    await _session(test_db, "x", a, pid)
    for sid in ("c0", "c1", "c2", "x"):
        await _msg(test_db, f"{sid}m", sid, f"words of {sid}")
    for caller in ("c0", "c2"):
        result = await _list(client, world["ka"], session_id=caller)
        assert _chat_ids(result) == ["x"]


async def test_branches_are_separate_rows_with_a_shared_thread(client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _doc(pid, "dA")
    await _session(test_db, "o", a, pid, document_id="dA", title="root chat")
    await _session(test_db, "o-b1", a, pid, document_id="dA", thread_id="o",
                   title="branch chat")
    await _msg(test_db, "om", "o", "root words")
    await _msg(test_db, "ob1m", "o-b1", "branch words")
    result = await _list(client, world["ka"])
    assert sorted(_chat_ids(result)) == ["o", "o-b1"]
    rows = {r["chat_id"]: r for r in result["rows"]}
    assert rows["o"]["thread_id"] == rows["o-b1"]["thread_id"] == "o"
    assert rows["o-b1"]["document_title"] == "doc dA"


# ─── list: narrowing, paging, order ──────────────────────────────────────────


async def test_under_document_id_narrows_to_the_subtree(client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _doc(pid, "dA")
    await _doc(pid, "dA1", "dA")
    await _doc(pid, "dB")
    for sid, did in (("sA", "dA"), ("sA1", "dA1"), ("sB", "dB"), ("sP", None)):
        await _session(test_db, sid, a, pid, **({"document_id": did} if did else {}))
    result = await _list(client, world["ka"], {"under_document_id": "dA"})
    assert sorted(_chat_ids(result)) == ["sA", "sA1"]


@pytest.mark.parametrize("root", ["no-such-doc", "dDeleted", "dForeign"])
async def test_under_document_root_unknown_deleted_or_foreign_is_404(
        client, test_db, world, root):
    await _doc(world["pid"], "dDeleted")
    await test_db.query(
        "UPDATE type::record('documents', 'dDeleted') SET deleted_at = time::now()")
    await _doc("other-project", "dForeign")
    await _list(client, world["ka"], {"under_document_id": root}, status=404)


async def test_title_contains_is_case_insensitive(client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "s1", a, pid, title="Diary of the Harbor")
    await _session(test_db, "s2", a, pid, title="glacier notes")
    assert _chat_ids(await _list(client, world["ka"],
                                 {"title_contains": "HARBOR"})) == ["s1"]
    assert (await _list(client, world["ka"], {"title_contains": "comet"}))["rows"] == []


async def test_limit_offset_next_offset_and_total_are_consistent(client, test_db, world):
    pid, a = world["pid"], world["a"]
    for i in range(5):
        await _session(test_db, f"s{i}", a, pid, **_at(T0))
        await _msg(test_db, f"s{i}m", f"s{i}", f"words {i}", **_at(T0))
    page1 = await _list(client, world["ka"], {"limit": 2})
    assert len(page1["rows"]) == 2 and page1["total"] == 5
    assert page1["next_offset"] == 2
    page2 = await _list(client, world["ka"], {"limit": 2, "offset": 2})
    assert page2["next_offset"] == 4
    page3 = await _list(client, world["ka"], {"limit": 2, "offset": 4})
    assert "next_offset" not in page3
    past = await _list(client, world["ka"], {"limit": 2, "offset": 20})
    assert (past["rows"], past["total"]) == ([], 5)


async def test_ordered_by_last_message_at_desc_and_dead_chats_sort_by_created(
        client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "old", a, pid, title="all deleted", **_at(T0))
    await _msg(test_db, "oldm", "old", "doomed", **_at(T0))
    await test_db.query(
        "UPDATE type::record('messages', 'oldm') SET deleted_at = time::now()")
    await _session(test_db, "mid", a, pid, **_at(T2))
    await _msg(test_db, "midm", "mid", "two live", **_at(T2))
    await _msg(test_db, "midm2", "mid", "one dead", role="assistant",
               parent_id="midm", **_at(T2))
    await test_db.query(
        "UPDATE type::record('messages', 'midm2') SET deleted_at = time::now()")
    await _session(test_db, "new", a, pid, **_at(T1))
    await _msg(test_db, "newm", "new", "latest words", **_at(T3))
    result = await _list(client, world["ka"])
    assert _chat_ids(result) == ["new", "mid", "old"]
    rows = {r["chat_id"]: r for r in result["rows"]}
    assert rows["new"]["message_count"] == 1
    assert rows["mid"]["message_count"] == 1  # the deleted reply is not counted
    assert rows["old"]["message_count"] == 0
    assert rows["old"]["last_message_at"] == rows["old"]["created_at"]


# ─── read: pages of whole turns ──────────────────────────────────────────────


async def _long_chat(db, a, pid) -> list[str]:
    await _session(db, "w", a, pid, title="diary")
    return await _chain(db, "w", [f"turn {i // 2} {('q', 'a')[i % 2]}" for i in range(10)],
                        "w")


async def test_read_pages_turns_to_the_end_without_loss_or_repeat(
        client, test_db, world):
    ids = await _long_chat(test_db, world["a"], world["pid"])
    seen: list[str] = []
    from_turn, pages = 0, 0
    while True:
        result = await _read(client, world["ka"], "w", from_turn)
        pages += 1
        assert result["chat_title"] == "diary" and result["total_turns"] == 5
        seen += [m["message_id"] for t in result["turns"] for m in t]
        from_turn = result.get("next_turn")
        if from_turn is None:
            break
        assert pages < 10
    assert seen == ids
    # every served message is whole and no marker leaks
    for turn in result["turns"]:
        for m in turn:
            assert "truncated" not in m and m["role"] in ("user", "assistant")


async def test_from_turn_past_the_end_is_an_empty_page(client, test_db, world):
    await _long_chat(test_db, world["a"], world["pid"])
    for from_turn in (5, 99):
        result = await _read(client, world["ka"], "w", from_turn)
        assert (result["turns"], result["total_turns"]) == ([], 5)
        assert "next_turn" not in result


async def test_deleted_messages_are_skipped(client, test_db, world):
    await _long_chat(test_db, world["a"], world["pid"])
    await test_db.query(
        "UPDATE type::record('messages', 'w1') SET deleted_at = time::now()")
    result = await _read(client, world["ka"], "w", 0)
    assert [m["message_id"] for t in result["turns"] for m in t] == [
        "w0", "w2", "w3", "w4", "w5", "w6", "w7", "w8", "w9"]


async def test_over_budget_message_is_truncated_and_search_returns_the_rest(
        client, test_db, world):
    pid, a = world["pid"], world["a"]
    await _session(test_db, "big", a, pid)
    body = "x" * 35000 + "TAIL"
    await _chain(test_db, "big", ["short question", body, "next q", "next a"], "big")
    page = await _read(client, world["ka"], "big", 0)
    assert len(page["turns"]) == 1
    big_msg = page["turns"][0][1]
    assert big_msg["message_id"] == "big1"
    assert big_msg["truncated"] is True
    # a message over the page budget BY ITSELF is cut at the page budget
    assert big_msg["next_offset"] == 30000
    assert big_msg["content"] == "x" * big_msg["next_offset"]
    assert page["next_turn"] == 1
    rest = await _post(client, world["ka"], "search_my_chats",
                       {"message_id": "big1", "offset": big_msg["next_offset"]})
    hit = next(m for m in rest["messages"] if m["message_id"] == "big1")
    assert hit["content"] == "x" * (35000 - big_msg["next_offset"]) + "TAIL"
    assert "next_offset" not in rest  # the message is done — nothing continues
    tail = await _read(client, world["ka"], "big", 1)
    assert [m["message_id"] for t in tail["turns"] for m in t] == ["big2", "big3"]
    assert "next_turn" not in tail


async def test_first_turn_nonlast_message_is_capped_and_turn_tail_survives(
        client, test_db, world):
    """F2 (security review): a huge NON-last message of the first served turn
    is capped at the page constant — never served whole — and the cap costs
    nothing: the turn's later messages are still served after the capped head,
    the page stops after the cut turn, and the capped tail is recoverable via
    `search_my_chats`."""
    pid, a = world["pid"], world["a"]
    await _session(test_db, "big2", a, pid)
    paste = "p" * 37000 + "END"
    await _chain(test_db, "big2", [paste, "short answer", "next q", "next a"], "big2")
    page = await _read(client, world["ka"], "big2", 0)
    head = page["turns"][0][0]
    assert head["message_id"] == "big20"
    assert head["truncated"] is True and head["next_offset"] == 30000
    assert head["content"] == "p" * 30000
    # the turn's tail is not orphaned: the answer is served after the capped head
    answer = page["turns"][0][1]
    assert (answer["message_id"], answer["content"]) == ("big21", "short answer")
    assert "truncated" not in answer
    # the page stops after the cut turn; the rest of the chat follows
    assert page["next_turn"] == 1
    rest = await _post(client, world["ka"], "search_my_chats",
                       {"message_id": "big20", "offset": head["next_offset"]})
    hit = next(m for m in rest["messages"] if m["message_id"] == "big20")
    assert hit["content"] == "p" * 7000 + "END"
    assert "next_offset" not in rest
    tail = await _read(client, world["ka"], "big2", 1)
    assert [m["message_id"] for t in tail["turns"] for m in t] == ["big22", "big23"]
    assert "next_turn" not in tail


async def test_read_refuses_every_invisible_chat_uniformly(client, test_db, world):
    pid, a, b = world["pid"], world["a"], world["b"]
    await _session(test_db, "bchat", b, pid)
    await _msg(test_db, "bchatm", "bchat", "b's private words")
    await _session(test_db, "hidden", a, pid)
    await test_db.query(
        "UPDATE type::record('chat_sessions', 'hidden') SET unindexed = true")
    await _msg(test_db, "hiddenm", "hidden", "hidden words")
    await _session(test_db, "gone", a, pid)
    await test_db.query(
        "UPDATE type::record('chat_sessions', 'gone') SET deleted_at = time::now()")
    await _session(test_db, "anote", a, pid, is_note=True)
    await _session(test_db, "conv", a, pid)
    await _msg(test_db, "convm", "conv", "current conversation words")
    for chat_id in ("bchat", "hidden", "gone", "anote", "conv", "no-such-chat"):
        result = await _read(client, world["ka"], chat_id, 0,
                             session_id="conv" if chat_id == "conv" else None,
                             status=404)
        assert result["detail"] == "chat not found"


# ─── both: the off-switch and the surfaces ───────────────────────────────────


async def test_external_key_is_refused_the_whole_pack_is_in_app_only(
        client, test_db, world):
    """Operator ruling 2026-10-24: the whole chat-recall pack (search, list,
    read) is closed to EXTERNAL keys — user-minted automation must not touch
    the person's chat corpus. Internally-minted keys (the in-app assistant's
    standing keys and its scoped run keys, both internal=True) stay."""
    pid, a = world["pid"], world["a"]
    await _session(test_db, "mine", a, pid, title="diary")
    await _msg(test_db, "minem", "mine", "hello")
    ext = await _key(a, pid, internal=False)
    for path, body in (
        ("search_my_chats", {"query": "hello"}),
        ("list_my_chats", {}),
        ("read_my_chat", {"chat_id": "mine", "from_turn": 0}),
    ):
        result = await _post(client, ext, path, body, status=403)
        assert result["detail"] == "chat-recall is available only to the in-app assistant"
    # an internal key still works
    assert _chat_ids(await _list(client, world["ka"])) == ["mine"]


@pytest.fixture
async def skill_disabled(test_db):
    """The admin's off-switch; removed after, since the shared cleanup keeps instance_skills."""
    await test_db.query(
        "CREATE instance_skills CONTENT {name: 'chat-recall', enabled: false, "
        "updated_by: 'admin'}")
    yield
    await test_db.query("DELETE instance_skills WHERE name = 'chat-recall'")


async def test_disabled_skill_refuses_both(client, world, skill_disabled):
    assert (await _list(client, world["ka"], status=403))["detail"].startswith(
        "chat-recall is disabled")
    await _read(client, world["ka"], "whatever", 0, status=403)


def test_both_tools_absent_from_mcp_present_on_agent_and_tool_api():
    from mcp_gateway.schemas import build_tool_list

    from main import app

    route_names = {
        r.path.removeprefix("/api/tool/")
        for r in app.routes
        if getattr(r, "path", "").startswith("/api/tool/")
        and "POST" in getattr(r, "methods", set()) and "{" not in r.path
    }
    for name in ("list_my_chats", "read_my_chat"):
        entry = by_name(name)
        assert entry.surfaces == {"agent", "tool_api"}  # derived from the registry
        assert entry not in mcp_entries()
        assert name not in {t.name for t in build_tool_list()}
        assert name in route_names
