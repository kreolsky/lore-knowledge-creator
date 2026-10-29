"""Branch-as-session primitives: POST /sessions/{id}/branches + GET /forks.

Plan chat-branch-sessions — the branch surface: a branch is its own
chat_sessions row bound for life to ONE dsh session (dsh id = Lore id),
the prefix rows are COPIED along the parent chain, and the lazy-seed stamp
(seed_source_session/seed_source_seq) names the (dsh session, seq) pair the
plugin's /session-fork seeds the branch's log from on its first turn.

Step 2 — the SWITCH: no leaf move before a turn, AI-chat message delete ⇒ 400, PATCH active_branch_id, thread
listing, and DELETE = the whole thread from its root.
"""


async def _mk_chat(client, token, pid, doc_id, **extra):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test", **extra},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _mk_msgs(test_db, sid, rows):
    """Real message rows keyed by id — the branch copy reads them via fetch_one."""
    for mid, fields in rows.items():
        await test_db.query(
            "CREATE type::record('messages', $id) CONTENT $f",
            {"id": mid, "f": {"chat_id": sid, "content": f"c-{mid}", **fields}},
        )


async def _session_row(test_db, sid):
    rows = await test_db.query(
        "SELECT * FROM type::record('chat_sessions', $id)", {"id": sid})
    return rows[0]


async def _branch_msgs(test_db, sid):
    return await test_db.query(
        "SELECT meta::id(id) AS mid, * OMIT images FROM messages "
        "WHERE chat_id = $cid AND deleted_at IS NONE",
        {"cid": sid},
    )


# ─── POST /sessions/{id}/branches: the copy ──────────────────────────────────


async def test_branch_refuses_an_unstamped_chain(
    client, admin_user, project_with_doc, test_db,
):
    """Assistant rows the driver never accounted (a pre-harness thread) are not
    a branch point — 422, exactly the old walk's answer."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    await _mk_msgs(test_db, sid, {
        "u1": {"role": "user", "parent_id": None},
        "a1": {"role": "assistant", "parent_id": "u1"},  # no driver_seq anywhere
    })

    resp = await client.post(
        f"/api/chat/sessions/{sid}/branches",
        json={"after_message_id": "a1"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422, resp.text
    assert "resolvable" in resp.text.lower()


async def test_branch_of_an_unseeded_branch_inherits_its_seed_stamp(
    client, admin_user, project_with_doc, test_db,
):
    """A rewind leaves a branch with NO dsh log until its first send; its
    copied rows name the branch itself as their session. A fork off it must
    seed from the log the copies' seqs live in — the branch's own stamp
    source — or the new branch could never run (the plugin 422s an absent
    source log). A half-stamped source passes its 422 marker on."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    dsh = f"lore-{sid}"
    await _mk_msgs(test_db, sid, {
        "u1": {"role": "user", "parent_id": None},
        "a1": {"role": "assistant", "parent_id": "u1",
               "driver_seq": 5, "driver_session": dsh},
        "u2": {"role": "user", "parent_id": "a1"},
    })
    resp = await client.post(
        f"/api/chat/sessions/{sid}/branches",
        json={"after_message_id": "u2"}, cookies={"lore_session": token})
    assert resp.status_code == 201, resp.text
    b_id = resp.json()["session_id"]
    a1_in_b = next(r for r in await _branch_msgs(test_db, b_id)
                   if r["origin_id"] == "a1")
    assert a1_in_b["driver_session"] == b_id  # the copy names the branch

    resp = await client.post(
        f"/api/chat/sessions/{b_id}/branches",
        json={"after_message_id": a1_in_b["mid"]},
        cookies={"lore_session": token})
    assert resp.status_code == 201, resp.text
    row = await _session_row(test_db, resp.json()["session_id"])
    assert row["seed_source_session"] == dsh
    assert row["seed_source_seq"] == 5

    await test_db.query(
        "UPDATE type::record('chat_sessions', $id) SET seed_source_seq = NONE",
        {"id": b_id})
    resp = await client.post(
        f"/api/chat/sessions/{b_id}/branches",
        json={"after_message_id": a1_in_b["mid"]},
        cookies={"lore_session": token})
    assert resp.status_code == 201, resp.text
    row = await _session_row(test_db, resp.json()["session_id"])
    assert row["seed_source_session"] == dsh
    assert row.get("seed_source_seq") is None


# ─── Step 2: PATCH active_branch_id (opening a branch = switching) ───────────


async def _thread_with_two_branches(client, token, test_db, pid, doc_id, tag=""):
    """A source chat u1→a1(stamped) plus a branch after a1. Returns
    (root_id, branch_id). `tag` namespaces the row ids: Surreal record ids
    are global per DB, so a SECOND thread inside one test needs distinct
    ids (the branch copy itself mints fresh ids)."""
    sid = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    u1, a1 = f"u1{tag}", f"a1{tag}"
    await _mk_msgs(test_db, sid, {
        u1: {"role": "user", "parent_id": None},
        a1: {"role": "assistant", "parent_id": u1,
             "driver_seq": 5, "driver_session": f"lore-{sid}"},
    })
    resp = await client.post(
        f"/api/chat/sessions/{sid}/branches",
        json={"after_message_id": a1},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return sid, resp.json()["session_id"]


async def test_patch_active_branch_refuses_a_foreign_thread(
    client, admin_user, project_with_doc, test_db,
):
    """The pointer must name a live branch of THIS thread — another thread's
    branch (or any dead id) is a hard 400, never a silent write."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, _branch = await _thread_with_two_branches(
        client, token, test_db, pid, doc_id)
    other_root, other_branch = await _thread_with_two_branches(
        client, token, test_db, pid, doc_id, tag="f")

    resp = await client.patch(
        f"/api/chat/sessions/{sid}",
        json={"active_branch_id": other_branch},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400, resp.text
    assert "live branch of this thread" in resp.json()["detail"]
    # The foreign root's own pointer is untouched too.
    assert (await _session_row(test_db, other_root)).get("active_branch_id") \
        == other_root


# ─── Step 2: the chat list counts THREADS ────────────────────────────────────


async def test_sessions_list_falls_back_to_the_root_when_the_pointer_is_dead(
    client, admin_user, project_with_doc, test_db,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, branch_id = await _thread_with_two_branches(
        client, token, test_db, pid, doc_id)
    await client.patch(
        f"/api/chat/sessions/{sid}",
        json={"active_branch_id": branch_id},
        cookies={"lore_session": token},
    )
    await test_db.query(
        "UPDATE type::record('chat_sessions', $id) SET deleted_at = time::now()",
        {"id": branch_id},
    )

    resp = await client.get(
        f"/api/chat/sessions?project_id={pid}&document_id={doc_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    listed = resp.json()
    assert [s["session_id"] for s in listed] == [sid]
