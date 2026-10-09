"""Branch-as-session primitives: POST /sessions/{id}/branches + GET /forks.

Plan chat-branch-sessions — the branch surface: a branch is its own
chat_sessions row bound for life to ONE dsh session (dsh id = Lore id),
the prefix rows are COPIED along the parent chain, and the lazy-seed stamp
(seed_source_session/seed_source_seq) names the (dsh session, seq) pair the
plugin's /session-fork seeds the branch's log from on its first turn.

Step 2 — the SWITCH: completions append linearly (non-tail parent ⇒ 409, no
leaf move), AI-chat message delete ⇒ 400, PATCH active_branch_id, thread
listing, and DELETE = the whole thread from its root.
"""
from uuid import uuid4


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


# ─── POST /sessions: every AI chat is its own thread ─────────────────────────


async def test_new_ai_chat_is_its_own_thread(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    out = await _mk_chat(client, token, pid, doc_id)

    assert out["thread_id"] == out["session_id"]
    assert out["active_branch_id"] == out["session_id"]


async def test_note_chat_gets_no_thread_columns(client, admin_user, project_with_doc):
    """Scope is AI chats only — a note keeps its reply tree untouched."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    out = await _mk_chat(client, token, pid, doc_id, is_note=True)

    assert out["thread_id"] is None
    assert out["active_branch_id"] is None


# ─── POST /sessions/{id}/branches: the copy ──────────────────────────────────


async def test_branch_copies_prefix_rows_with_origin_chain_and_driver_seq(
    client, admin_user, project_with_doc, test_db,
):
    """Copy fidelity: the branch holds the root→after chain as NEW rows with
    NEW ids, parent links re-pointed onto the copies, driver_seq preserved
    (the seed keeps parent seqs), driver_session re-stamped to the BRANCH id,
    and origin_id = the SOURCE row's origin (or its own id) — the switcher
    compares origins, never row ids."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    dsh = f"lore-{sid}"
    await _mk_msgs(test_db, sid, {
        "u1": {"role": "user", "parent_id": None},
        "a1": {"role": "assistant", "parent_id": "u1",
               "driver_seq": 5, "driver_session": dsh},
        "u2": {"role": "user", "parent_id": "a1"},
        "a2": {"role": "assistant", "parent_id": "u2",
               "driver_seq": 9, "driver_session": f"{dsh}~f1"},
    })

    resp = await client.post(
        f"/api/chat/sessions/{sid}/branches",
        json={"after_message_id": "u2"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    new_sid = body["session_id"]

    # The session row: thread identity + provenance + the lazy-seed stamp
    # (seq and source off the SAME row — a1's pair), and NO compacted_from
    # copy (that column names the dsh log this chat's turns run in).
    assert body["thread_id"] == sid
    assert body["forked_from_session"] == sid
    assert body["forked_after_origin"] == "u2"
    assert body["active_branch_id"] == new_sid
    row = await _session_row(test_db, new_sid)
    assert row["seed_source_session"] == dsh
    assert row["seed_source_seq"] == 5
    assert row.get("compacted_from") is None
    assert row["project_id"] == pid
    assert row["user_id"] == (await _session_row(test_db, sid))["user_id"]

    # The copied chain: u1 → a1 → u2, re-pointed, origin-tagged, seq kept.
    copies = {r["mid"]: r for r in await _branch_msgs(test_db, new_sid)}
    assert len(copies) == 3
    by_origin = {r["origin_id"]: r for r in copies.values()}
    assert set(by_origin) == {"u1", "a1", "u2"}
    # .get, not []: SurrealDB DROPS a field whose CONTENT value is null, so
    # the root copy carries NO parent_id key at all.
    assert by_origin["u1"].get("parent_id") is None
    assert by_origin["a1"]["parent_id"] == by_origin["u1"]["mid"]
    assert by_origin["u2"]["parent_id"] == by_origin["a1"]["mid"]
    for r in copies.values():
        assert r["chat_id"] == new_sid
        assert r["driver_session"] == new_sid
        assert r["content"] == f"c-{r['origin_id']}"
    assert by_origin["a1"]["driver_seq"] == 5
    assert by_origin["u1"].get("driver_seq") is None


async def test_branch_of_a_user_only_chain_seeds_nothing(
    client, admin_user, project_with_doc, test_db,
):
    """A prefix with no completed turn (the root branch of the walk — resolvable
    with seq None) has NOTHING to seed: the branch's dsh log starts empty under
    its own id on the first turn, so no seed_source_* stamp is written."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    await _mk_msgs(test_db, sid, {"u1": {"role": "user", "parent_id": None}})

    resp = await client.post(
        f"/api/chat/sessions/{sid}/branches",
        json={"after_message_id": "u1"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    row = await _session_row(test_db, resp.json()["session_id"])
    assert row.get("seed_source_session") is None
    assert row.get("seed_source_seq") is None


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


async def test_branch_refuses_a_note_session(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    out = await _mk_chat(client, token, pid, doc_id, is_note=True)

    resp = await client.post(
        f"/api/chat/sessions/{out['session_id']}/branches",
        json={"after_message_id": "whatever"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400, resp.text


async def test_branch_refuses_a_null_after_message_id(
    client, admin_user, project_with_doc,
):
    """The first message of an AI chat is immutable — there is no root-level
    fork, so a missing or explicit-null after_message_id is a 400, never a
    root branch."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _mk_chat(client, token, pid, doc_id))["session_id"]

    for payload in ({}, {"after_message_id": None}):
        resp = await client.post(
            f"/api/chat/sessions/{sid}/branches",
            json=payload,
            cookies={"lore_session": token},
        )
        assert resp.status_code == 400, (payload, resp.text)


async def test_branch_and_forks_refuse_a_non_owner(
    client, admin_user, regular_user, project_with_doc, test_db,
):
    """Per-row session access stays: a non-owner (404 — never leaks the
    session's existence) cannot branch another user's chat nor read its
    fork options."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    _, other_token = regular_user
    sid = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    await _mk_msgs(test_db, sid, {
        "u1": {"role": "user", "parent_id": None},
        "a1": {"role": "assistant", "parent_id": "u1",
               "driver_seq": 3, "driver_session": f"lore-{sid}"},
    })

    resp = await client.post(
        f"/api/chat/sessions/{sid}/branches",
        json={"after_message_id": "a1"},
        cookies={"lore_session": other_token},
    )
    assert resp.status_code == 404, resp.text

    resp = await client.get(
        f"/api/chat/sessions/{sid}/forks",
        cookies={"lore_session": other_token},
    )
    assert resp.status_code == 404, resp.text


async def test_branch_falls_back_to_the_chats_dsh_id_for_a_legacy_row(
    client, admin_user, project_with_doc, test_db,
):
    """A row stamped before the (seq, session) pair carries no driver_session:
    the seed source falls back to the chat's own dsh id (compacted_from or id)
    — the id the plugin's SessionMap resolves for legacy chats."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    await _mk_msgs(test_db, sid, {
        "u1": {"role": "user", "parent_id": None},
        "a1": {"role": "assistant", "parent_id": "u1", "driver_seq": 7},
    })

    resp = await client.post(
        f"/api/chat/sessions/{sid}/branches",
        json={"after_message_id": "a1"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    row = await _session_row(test_db, resp.json()["session_id"])
    assert row["seed_source_session"] == sid
    assert row["seed_source_seq"] == 7


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


# ─── GET /sessions/{id}/forks: the switcher projection ───────────────────────


async def _forked_thread(client, token, test_db, pid, doc_id):
    """A source chat u1→a1→u2 with TWO continuations after u2: the source's
    own e2 and a branch's e1 (inserted as rows — the turns that mint them are
    step 2's flow). Returns (sid, branch_id, e1_origin, e2_origin)."""
    sid = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    await _mk_msgs(test_db, sid, {
        "u1": {"role": "user", "parent_id": None},
        "a1": {"role": "assistant", "parent_id": "u1",
               "driver_seq": 5, "driver_session": f"lore-{sid}"},
        "u2": {"role": "user", "parent_id": "a1"},
    })
    resp = await client.post(
        f"/api/chat/sessions/{sid}/branches",
        json={"after_message_id": "u2"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    branch_id = resp.json()["session_id"]
    u2_copy = next(
        r for r in await _branch_msgs(test_db, branch_id)
        if r["origin_id"] == "u2")
    e1, e2 = str(uuid4()), str(uuid4())
    await _mk_msgs(test_db, branch_id, {
        e1: {"role": "user", "parent_id": u2_copy["mid"]}})
    await _mk_msgs(test_db, sid, {
        e2: {"role": "user", "parent_id": "u2"}})
    return sid, branch_id, e1, e2


def _options(entry):
    """An entry's options as (session, next origin, current) — the created_at
    values are server clocks, the ORDER is what the switcher reads."""
    return [(o["session_id"], o["next_origin"], o["current"])
            for o in entry["options"]]


async def test_forks_lists_every_continuation_of_each_origin(
    client, admin_user, project_with_doc, test_db,
):
    """For every origin P on the branch with two or more DISTINCT
    continuations: one option per continuation, this session's own flagged
    `current`, in the same order (earliest session first) from either side —
    so both branches agree on "1/2" vs "2/2"."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, branch_id, e1, e2 = await _forked_thread(
        client, token, test_db, pid, doc_id)

    resp = await client.get(
        f"/api/chat/sessions/{sid}/forks", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert [e["after_origin"] for e in out] == ["u2"]
    assert _options(out[0]) == [(sid, e2, True), (branch_id, e1, False)]

    resp = await client.get(
        f"/api/chat/sessions/{branch_id}/forks", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert [e["after_origin"] for e in out] == ["u2"]
    assert _options(out[0]) == [(sid, e2, False), (branch_id, e1, True)]


async def test_forks_count_continuations_not_sessions_after_a_fork_off_a_fork(
    client, admin_user, project_with_doc, test_db,
):
    """A fork off a fork shares its parent's continuation at the OUTER fork
    point: two continuations there, never three sessions — and the shared one
    opens the viewer's own branch when it is the viewer's, else the newest."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, b_id, e1, e2 = await _forked_thread(client, token, test_db, pid, doc_id)
    resp = await client.post(
        f"/api/chat/sessions/{b_id}/branches",
        json={"after_message_id": e1},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    c_id = resp.json()["session_id"]
    e1_in_c = next(r for r in await _branch_msgs(test_db, c_id)
                   if r["origin_id"] == e1)
    f1, f2 = str(uuid4()), str(uuid4())
    await _mk_msgs(test_db, c_id, {f1: {"role": "user", "parent_id": e1_in_c["mid"]}})
    await _mk_msgs(test_db, b_id, {f2: {"role": "user", "parent_id": e1}})

    by_p = {e["after_origin"]: e
            for e in (await client.get(
                f"/api/chat/sessions/{c_id}/forks",
                cookies={"lore_session": token})).json()}
    assert _options(by_p["u2"]) == [(sid, e2, False), (c_id, e1, True)]
    assert _options(by_p[e1]) == [(b_id, f2, False), (c_id, f1, True)]

    by_p = {e["after_origin"]: e
            for e in (await client.get(
                f"/api/chat/sessions/{sid}/forks",
                cookies={"lore_session": token})).json()}
    assert list(by_p) == ["u2"]
    assert _options(by_p["u2"]) == [(sid, e2, True), (c_id, e1, False)]


async def test_forks_empty_without_a_divergent_continuation(
    client, admin_user, project_with_doc, test_db,
):
    """A branch that shares every continuation with the source (nothing sent
    yet on either side) has no fork to offer."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    await _mk_msgs(test_db, sid, {
        "u1": {"role": "user", "parent_id": None},
        "a1": {"role": "assistant", "parent_id": "u1",
               "driver_seq": 5, "driver_session": f"lore-{sid}"},
    })
    resp = await client.post(
        f"/api/chat/sessions/{sid}/branches",
        json={"after_message_id": "a1"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text

    for target in (sid, resp.json()["session_id"]):
        resp = await client.get(
            f"/api/chat/sessions/{target}/forks", cookies={"lore_session": token})
        assert resp.status_code == 200, resp.text
        assert resp.json() == []


async def test_forks_refuses_a_note_session(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    out = await _mk_chat(client, token, pid, doc_id, is_note=True)

    resp = await client.get(
        f"/api/chat/sessions/{out['session_id']}/forks",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400, resp.text


# ─── Step 2: the linear-turn contract (completions) ──────────────────────────


async def _linear_chat(test_db, client, token, pid, doc_id):
    """u1 → a1(stamped) → u2 — a live linear chain; u2 is the tail."""
    sid = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    await _mk_msgs(test_db, sid, {
        "u1": {"role": "user", "parent_id": None},
        "a1": {"role": "assistant", "parent_id": "u1",
               "driver_seq": 5, "driver_session": f"lore-{sid}"},
        "u2": {"role": "user", "parent_id": "a1"},
    })
    return sid


async def test_completion_refuses_a_non_tail_parent_with_409(
    client, admin_user, project_with_doc, test_db,
):
    """A turn's parent must name the session's live TAIL — a live-but-stale
    parent is a conflict (409: re-read and retry), never a silent fork."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _linear_chat(test_db, client, token, pid, doc_id)

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json={"messages": [{"role": "user", "content": "x"}], "parent_id": "u1"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 409, resp.text
    assert "not the tail" in resp.json()["detail"]
    # The check runs under the turn lock, and a refusal releases it — a
    # stale client never wedges the chat until the lock TTL.
    import turn_lock
    assert not await turn_lock.turn_lock_held(sid)


async def test_completion_refuses_a_null_parent_on_a_non_empty_session(
    client, admin_user, project_with_doc, test_db,
):
    """The first message is immutable — a null parent on a session with rows
    is a stale client (the old root-fork reset is gone), answered 409."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _linear_chat(test_db, client, token, pid, doc_id)

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json={"messages": [{"role": "user", "content": "x"}]},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 409, resp.text
    assert "not the tail" in resp.json()["detail"]


async def test_completion_refuses_a_foreign_parent_with_422(
    client, admin_user, project_with_doc, test_db,
):
    """A parent that is not a live row of THIS session is a corrupt chain —
    the same honest 422 the old seam-A walk gave."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _linear_chat(test_db, client, token, pid, doc_id)

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json={"messages": [{"role": "user", "content": "x"}],
              "parent_id": str(uuid4())},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422, resp.text
    assert "resolvable" in resp.text.lower()


# ─── Step 2: AI-chat message delete is refused ────────────────────────────────


async def test_message_delete_is_refused_on_an_ai_chat(
    client, admin_user, project_with_doc, test_db,
):
    """A branch only appends — there is no message surgery on an AI chat."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _mk_chat(client, token, pid, doc_id))["session_id"]
    await _mk_msgs(test_db, sid, {"u1": {"role": "user", "parent_id": None}})

    resp = await client.delete(
        "/api/chat/messages/u1", cookies={"lore_session": token})
    assert resp.status_code == 400, resp.text
    assert "delete the conversation" in resp.json()["detail"]
    # The row is intact (a refusal, not a delete-then-error).
    rows = await _branch_msgs(test_db, sid)
    assert len(rows) == 1


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


async def test_patch_active_branch_on_the_thread_root(
    client, admin_user, project_with_doc, test_db,
):
    """Opening a branch = switching: the ROOT's active_branch_id names the
    last-opened branch (the chat list previews it)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, branch_id = await _thread_with_two_branches(
        client, token, test_db, pid, doc_id)

    resp = await client.patch(
        f"/api/chat/sessions/{sid}",
        json={"active_branch_id": branch_id},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["active_branch_id"] == branch_id
    assert (await _session_row(test_db, sid))["active_branch_id"] == branch_id


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


async def test_patch_active_branch_refuses_a_deleted_branch(
    client, admin_user, project_with_doc, test_db,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, branch_id = await _thread_with_two_branches(
        client, token, test_db, pid, doc_id)
    await test_db.query(
        "UPDATE type::record('chat_sessions', $id) SET deleted_at = time::now()",
        {"id": branch_id},
    )

    resp = await client.patch(
        f"/api/chat/sessions/{sid}",
        json={"active_branch_id": branch_id},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400, resp.text


# ─── Step 2: the chat list counts THREADS ────────────────────────────────────


async def test_sessions_list_counts_threads_and_previews_the_active_branch(
    client, admin_user, project_with_doc, test_db,
):
    """One entry per thread: the item is the active_branch_id row (its fields,
    its last-activity), and branches never eat the candidate cap."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, branch_id = await _thread_with_two_branches(
        client, token, test_db, pid, doc_id)
    # A message on the BRANCH: the preview/last-activity must follow it.
    await _mk_msgs(test_db, branch_id, {
        "e1": {"role": "user", "parent_id": None},
    })
    # created_at is a typed datetime field — a string in CONTENT is refused;
    # set it through a SurrealQL datetime literal.
    await test_db.query(
        "UPDATE type::record('messages', $id) SET created_at = d'2026-01-02T00:00:00Z'",
        {"id": "e1"},
    )
    resp = await client.patch(
        f"/api/chat/sessions/{sid}",
        json={"active_branch_id": branch_id},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/api/chat/sessions?project_id={pid}&document_id={doc_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    listed = resp.json()
    assert isinstance(listed, list) and len(listed) == 1, listed
    assert listed[0]["session_id"] == branch_id
    assert listed[0]["thread_id"] == sid
    # The last-activity aggregate ran over the BRANCH's messages (e1), not
    # the root's.
    assert listed[0]["last_message_at"]


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


# ─── Step 2: DELETE = the whole thread, root-only ────────────────────────────


async def test_delete_refuses_a_branch_id(
    client, admin_user, project_with_doc, test_db,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, branch_id = await _thread_with_two_branches(
        client, token, test_db, pid, doc_id)

    resp = await client.delete(
        f"/api/chat/sessions/{branch_id}", cookies={"lore_session": token})
    assert resp.status_code == 400, resp.text
    assert "delete the conversation, not a branch" in resp.json()["detail"]
    # Nothing died: both rows live.
    for row_id in (sid, branch_id):
        assert (await _session_row(test_db, row_id)).get("deleted_at") is None


async def test_delete_root_soft_deletes_every_thread_row_and_tears_down_each_side(
    client, admin_user, project_with_doc, test_db, monkeypatch,
):
    """Root delete = per-row delete + teardown over the WHOLE thread: every
    branch owns its dsh session, its agent key and its fan-out."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, branch_id = await _thread_with_two_branches(
        client, token, test_db, pid, doc_id)

    fanout_calls: list = []
    key_calls: list = []
    ask_calls: list = []

    async def _fake_stop_fanout(session_id, driver_session_id=None):
        fanout_calls.append(session_id)

    async def _fake_revoke(session_id):
        key_calls.append(session_id)

    async def _fake_asks(session_id, reason=None):
        ask_calls.append(session_id)

    monkeypatch.setattr("routes.chat.fanout.stop_fanout", _fake_stop_fanout)
    monkeypatch.setattr("agent.keys.revoke_session_agent_key", _fake_revoke)
    monkeypatch.setattr("routes.chat.verdicts.resolve_session_asks", _fake_asks)

    resp = await client.delete(
        f"/api/chat/sessions/{sid}", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text

    # Every row of the thread soft-deleted (root + branch), messages with them.
    for row_id in (sid, branch_id):
        assert (await _session_row(test_db, row_id)).get("deleted_at") is not None
        assert await _branch_msgs(test_db, row_id) == []
    # Teardown ran ONCE PER ROW, each under its own id.
    assert sorted(fanout_calls) == sorted([sid, branch_id])
    assert sorted(key_calls) == sorted([sid, branch_id])
    assert sorted(ask_calls) == sorted([sid, branch_id])
