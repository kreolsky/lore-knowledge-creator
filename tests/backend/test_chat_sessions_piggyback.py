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


async def test_without_flag_returns_plain_list(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    await _create_session(client, token, pid, doc_id)
    data = await _list(client, token, pid, doc_id)
    assert isinstance(data, list)


async def test_piggyback_returns_preferred_session_messages(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)
    await _post_message(client, token, sid, "hello")

    data = await _list(client, token, pid, doc_id,
                       with_active_messages="true", preferred_session_id=sid)
    assert isinstance(data, dict)
    assert {s["session_id"] for s in data["sessions"]} >= {sid}
    am = data["active_messages"]
    assert am["session_id"] == sid
    assert [m["content"] for m in am["messages"]] == ["hello"]


async def test_piggyback_messages_omit_images(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)
    await _post_message(client, token, sid, "pic", images=["data:image/png;base64,iVBORw0KGgo="])

    data = await _list(client, token, pid, doc_id,
                       with_active_messages="true", preferred_session_id=sid)
    msg = data["active_messages"]["messages"][0]
    assert "images" not in msg, "piggyback must reuse the images-omitting projection"
    assert msg["image_count"] == 1


async def test_no_preferred_id_returns_null_active_messages(client, admin_user, project_with_doc):
    """Decision 4: with no preferred id the backend returns active_messages=NULL.
    The empty-hint (page[0]) fallback is removed — project-wide page[0] no longer
    matches the resolver's doc-owned pick_first, so piggybacking it would almost
    never match. The client does a normal second loadMessages on the cold path."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    s_old = await _create_session(client, token, pid, doc_id)
    s_new = await _create_session(client, token, pid, doc_id)
    # Give s_old the most recent activity so it sorts FIRST (sort is by last message).
    await _post_message(client, token, s_new, "older")
    await _post_message(client, token, s_old, "newer")

    data = await _list(client, token, pid, doc_id, with_active_messages="true")
    assert data["active_messages"] is None


async def test_stale_preferred_id_returns_null_active_messages(client, admin_user, project_with_doc):
    """Decision 4: a preferred id absent from the (capped) page yields NULL — no
    fallback to page[0]. The client's second loadMessages resolves the active chat."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)
    await _post_message(client, token, sid, "hi")

    data = await _list(client, token, pid, doc_id,
                       with_active_messages="true", preferred_session_id="does-not-exist")
    assert data["active_messages"] is None


async def _insert_stamped_assistant(sid, parent_id, seq=7):
    from db import get_db

    db = await get_db()
    await db.query(
        "CREATE type::record('messages', $id) SET chat_id = $cid, role = 'assistant', "
        "content = 'answer', parent_id = $pid, driver_seq = $seq, "
        "created_at = time::now()",
        {"id": f"assistant-{seq}", "cid": sid, "pid": parent_id, "seq": seq},
    )


def _pin_entries(monkeypatch, turns):
    import driver.timeline

    async def fake_fetch(_session_id):
        # The ReplayedSession mapping — a truthy LIST here would crash
        # _fetch_session_turns (**(replay or {}) needs a mapping).
        return {"turns": turns, "tail_seq": None}

    monkeypatch.setattr(driver.timeline, "fetch_session_entries", fake_fetch)


FRAMES = [{"type": "dsh_event", "kind": "assistant/message", "seq": 5,
           "surfaceOp": "append",
           "data": {"turn": 1, "step": 1,
                    "message": {"role": "assistant",
                                "content": [{"type": "text", "text": "answer"}]}}}]


async def test_piggyback_carries_the_agent_timeline(
    client, admin_user, project_with_doc, monkeypatch,
):
    """# INVARIANT (timeline parity): every path that returns a session's rows
    returns the SAME timeline. Why: the piggyback is the restore path of a page
    reload / second tab — committing rows without `frames` rendered the session
    text-only until a manual re-select, exactly the live-vs-reload divergence
    the read path exists to prevent (observed: same row frames=104 via
    GET /messages, frames ABSENT via the piggyback)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)
    user_id = (await _post_message(client, token, sid, "hello"))["message_id"]
    await _insert_stamped_assistant(sid, user_id)
    _pin_entries(monkeypatch, [{"end_seq": 7, "frames": FRAMES}])

    data = await _list(client, token, pid, doc_id,
                       with_active_messages="true", preferred_session_id=sid)
    rows = data["active_messages"]["messages"]
    assistant = next(m for m in rows if m["role"] == "assistant")
    assert assistant["frames"] == FRAMES, (
        "the piggyback must carry the same timeline GET /messages does"
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
