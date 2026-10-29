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

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


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


# ─── read: pages of whole turns ──────────────────────────────────────────────


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
