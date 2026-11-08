"""chat facts — moderated capture from THIS conversation (plan chat-facts, step 1).

The two server-side contracts the plan names: the full-access wall on
`save_chat_facts`, and the privacy filter in retrieval's fact payload — a
chat-sourced fact never carries its (private) chat title to another member
through `h.sources`.
"""
import hashlib
import secrets


async def _key(user_id: str, project_id: str) -> str:
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"agent-key-{secrets.token_hex(4)}", {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent", "capabilities": ["agent"], "internal": True,
    })
    return token


async def _session(db, sid, uid, pid, **fields):
    await db.query(
        "CREATE type::record('chat_sessions', $id) CONTENT $f",
        {"id": sid, "f": {"user_id": uid, "project_id": pid, "title": f"title {sid}",
                          "model": "m", **fields}},
    )


# TEST-WHY: refused-principal — a commentator member with their own live session and a valid internal agent key is refused save_chat_facts (403) before any run key is minted or fact written: saving from a chat publishes project-wide memory, so the Tool-API write gate must hold even though the pack's read tools serve commentators.
async def test_commentator_member_is_refused_save_chat_facts(client, test_db, project_with_doc, regular_user):
    pid, _idx, _uid = project_with_doc
    member_uid = regular_user[0]
    await test_db.query(
        "CREATE type::record('project_members', $id) SET project_id = $pid, "
        "user_id = $uid, access_level = 'commentator'",
        {"id": f"pm-cf-{pid}-{member_uid}", "pid": pid, "uid": member_uid},
    )
    await _session(test_db, "cf-src", member_uid, pid)
    token = await _key(member_uid, pid)
    resp = await client.post(
        "/api/tool/save_chat_facts",
        json={"verdicts": [
            {"action": "new", "title": "S: gist", "text": "one assertion"},
        ]},
        headers={"Authorization": f"Bearer {token}",
                 "X-Agent-Session-Id": "cf-src"},
    )
    assert resp.status_code == 403, resp.text
    assert "Full project access required" in resp.json()["detail"]


# TEST-WHY: cross-process-contract — the fact payload's source titles over real Surreal rows: a chat-sourced fact is served with EMPTY sources (the private chat's title never rides a fact hit to another member — _load_fact_payloads titles kind "reference" only), while a reference-sourced fact still gets its reference titled.
async def test_chat_sourced_fact_serves_no_chat_title_through_sources(test_db, project_with_doc):
    from db import create_record
    from retrieval import RetrievalHit, _load_fact_payloads

    pid, _idx, _uid = project_with_doc
    secret_title = "SECRET CHAT TITLE zq7"
    await _session(test_db, "cf-leak", _uid, pid, title=secret_title)
    await create_record("documents", "cf-host", {
        "project_id": pid, "parent_id": None, "title": "Host",
        "content": "", "path": "cf-host", "is_reference": False,
    })
    await create_record("documents", "cf-ref", {
        "project_id": pid, "parent_id": "cf-host", "title": "Ref Titled",
        "content": "", "path": "cf-ref", "is_reference": True,
    })

    async def fact(doc_id, sources):
        await create_record("documents", doc_id, {
            "project_id": pid, "parent_id": None, "title": doc_id,
            "content": "the fact body", "path": doc_id,
            "is_memory": True, "mem_active": True,
            "mem": {"provenance": {"sources": sources}},
        })
        return doc_id

    chat_fact = await fact(
        "cf-fact-chat", [{"kind": "chat", "id": "cf-leak"}])
    ref_fact = await fact(
        "cf-fact-ref", [{"kind": "reference", "id": "cf-ref"}])

    def hit(doc_id):
        return RetrievalHit(
            kind="memory", parent_id=doc_id, parent_title="", heading=None,
            snippet="", offset_start=None, offset_end=None, score=1.0,
        )

    chat_hit, ref_hit = await _load_fact_payloads(
        test_db, [hit(chat_fact), hit(ref_fact)],
    )
    assert chat_hit.sources == [], (
        f"a chat-sourced fact rode its chat title out: {chat_hit.sources!r}"
    )
    assert secret_title not in repr(chat_hit.sources)
    assert ref_hit.sources == [{"id": "cf-ref", "title": "Ref Titled"}], (
        "the reference arm lost its titled source"
    )
