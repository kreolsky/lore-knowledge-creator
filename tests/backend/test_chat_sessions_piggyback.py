"""Item 3 — collapse the sessions→messages waterfall.

GET /chat/sessions?with_active_messages=true piggybacks the messages of the session
the client will make active (preferred_session_id). With a valid preferred id the
panel opens in one round-trip; with NO hint (or a stale one) the piggyback is NULL
and the client does a normal second loadMessages (Decision 4 — the page[0] fallback
was removed because project-wide page[0] no longer matches the resolver's doc-owned
pick_first).
"""


async def _create_session(client, token, pid, doc_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def _post_message(client, token, sid, content, images=None):
    body = {"role": "user", "content": content}
    if images is not None:
        body["images"] = images
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages", json=body,
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _list(client, token, pid, doc_id, **params):
    q = {"project_id": pid, "document_id": doc_id, **params}
    resp = await client.get("/api/chat/sessions", params=q, cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _insert_stamped_assistant(sid, parent_id, seq=7):
    from db import get_db

    db = await get_db()
    await db.query(
        "CREATE type::record('messages', $id) SET chat_id = $cid, role = 'assistant', "
        "content = 'answer', parent_id = $pid, driver_seq = $seq, "
        "created_at = time::now()",
        {"id": f"assistant-{seq}", "cid": sid, "pid": parent_id, "seq": seq},
    )


async def test_piggyback_timeline_outage_returns_null_active_messages(
    client, admin_user, project_with_doc, monkeypatch,
):
    """An unreadable transcript must not take the session INDEX down (502 on
    GET /chat/sessions), and must not silently degrade either: the piggyback
    drops to NULL so the client's null-branch falls back to loadMessages —
    which surfaces the named 502."""
    from driver.timeline import DriverTimelineUnavailable

    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)
    user_id = (await _post_message(client, token, sid, "hello"))["message_id"]
    await _insert_stamped_assistant(sid, user_id)

    async def fake_fetch(_session_id):
        raise DriverTimelineUnavailable("harness", "down")

    import driver.timeline

    monkeypatch.setattr(driver.timeline, "fetch_session_entries", fake_fetch)

    data = await _list(client, token, pid, doc_id,
                       with_active_messages="true", preferred_session_id=sid)
    assert data["active_messages"] is None
    assert any(s["session_id"] == sid for s in data["sessions"]), (
        "the index itself must survive a transcript outage"
    )
