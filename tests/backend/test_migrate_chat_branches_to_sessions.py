"""The chat_branches_to_sessions migration (plan chat-branch-sessions, step 3).

A legacy AI chat was ONE chat_sessions row whose messages formed a tree while
the dsh log held only the ACTIVE lineage. The migration carves every
non-active leaf lineage out as its own branch session (a COPY of its whole
root→leaf chain, origins preserved, stamped for the lazy /session-fork seed),
soft-deletes the abandoned rows from the original, and self-threads every
legacy AI chat (thread_id = active_branch_id = own id). The active line's
rows, the session id, and therefore its live dsh mapping stay untouched.

Fixture trees per the plan's Order 3: 3 flat branches; a fork off a
non-active branch (nested — both carved branches carry the shared segment,
/forks shows the inner fork); an unstamped branch (the 422 marker); two
root-level first messages (forked_after_origin NONE, /forks P=None).
"""
from uuid import uuid4

from migrations.migrate_chat_branches_to_sessions import (
    _migrate_chat_branches_to_sessions,
)


async def _mk_chat(client, token, pid, doc_id, **extra):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test", **extra},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def _legacy_chat(client, token, pid, doc_id, test_db, **extra):
    """A chat as the pre-migration world left it: no thread columns."""
    sid = await _mk_chat(client, token, pid, doc_id, **extra)
    await test_db.query(
        "UPDATE type::record('chat_sessions', $id) "
        "SET thread_id = NONE, active_branch_id = NONE",
        {"id": sid},
    )
    return sid


class _Tree:
    """A legacy message-tree builder: rows keyed by short names, created with
    increasing created_at so newest/stamped-head picks are deterministic.

    Surreal record ids are global per DB, so every row id is uuid-namespaced
    (the `_thread_with_two_branches` lesson): the SAME test body runs against
    a DB other tests already filled, and the migration's second (idempotency)
    run must not collide with the first run's copies either."""

    def __init__(self, test_db, sid):
        self.db = test_db
        self.sid = sid
        self.ids: dict[str, str] = {}
        self._tick = 0

    def mid(self, name: str) -> str:
        if name not in self.ids:
            self.ids[name] = f"{name}-{uuid4().hex[:10]}"
        return self.ids[name]

    async def row(self, name: str, parent, role, **extra):
        self._tick += 1
        mid = self.mid(name)
        await self.db.query(
            "CREATE type::record('messages', $id) CONTENT $f",
            {"id": mid, "f": {
                "chat_id": self.sid, "role": role,
                "parent_id": self.mid(parent) if parent else None,
                "content": f"c-{name}", **extra,
            }},
        )
        # created_at is a typed datetime field — a string in CONTENT (and a
        # string param) is refused; set it through a SurrealQL d'...' literal
        # (the `_thread_with_two_branches` precedent).
        await self.db.query(
            f"UPDATE type::record('messages', $id) "
            f"SET created_at = d'2026-01-01T00:00:{self._tick:02d}Z'",
            {"id": mid},
        )

    async def live_mids(self) -> list[str]:
        rows = await self.db.query(
            "SELECT meta::id(id) AS mid FROM messages "
            "WHERE chat_id = $cid AND deleted_at IS NONE",
            {"cid": self.sid},
        )
        return sorted(r["mid"] for r in rows)


async def _branch_sessions(test_db, sid):
    rows = await test_db.query(
        "SELECT meta::id(id) AS bid, * OMIT anchor_rel_start, anchor_rel_end "
        "FROM chat_sessions WHERE thread_id = $tid AND deleted_at IS NONE",
        {"tid": sid},
    )
    return {r["bid"]: r for r in rows if r["bid"] != sid}


async def _branch_rows(test_db, bid):
    rows = await test_db.query(
        "SELECT meta::id(id) AS mid, * OMIT images FROM messages "
        "WHERE chat_id = $cid AND deleted_at IS NONE",
        {"cid": bid},
    )
    return sorted(rows, key=lambda r: (r.get("created_at") or "", r["mid"]))


async def _forks(client, token, sid):
    resp = await client.get(
        f"/api/chat/sessions/{sid}/forks", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ─── the flat fixture: 3 branches after one fork point ───────────────────────


async def _flat_tree(client, token, test_db, pid, doc_id):
    """u1→a1(stamped)→u2 with THREE continuations after u2: the source's own
    e2→A2 (newest stamped ⇒ active) and two abandoned leaves."""
    sid = await _legacy_chat(client, token, pid, doc_id, test_db)
    t = _Tree(test_db, sid)
    await t.row("u1", None, "user")
    await t.row("a1", "u1", "assistant", driver_seq=5, driver_session="L0")
    await t.row("u2", "a1", "user")
    await t.row("e1a", "u2", "user")
    await t.row("A1a", "e1a", "assistant", driver_seq=9, driver_session="L1")
    await t.row("e1b", "u2", "user")
    await t.row("A1b", "e1b", "assistant", driver_seq=11, driver_session="L2")
    await t.row("e2", "u2", "user")
    await t.row("A2", "e2", "assistant", driver_seq=13, driver_session="L3")
    return sid, t


async def test_flat_tree_carves_each_non_active_leaf_with_its_whole_lineage(
    client, admin_user, project_with_doc, test_db,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, t = await _flat_tree(client, token, test_db, pid, doc_id)

    await _migrate_chat_branches_to_sessions(test_db)

    # The original self-threads and keeps ONLY its active line.
    root = (await test_db.query(
        "SELECT * FROM type::record('chat_sessions', $id)", {"id": sid}))[0]
    assert root["thread_id"] == sid
    assert root["active_branch_id"] == sid
    assert await t.live_mids() == sorted(t.mid(n) for n in ("u1", "a1", "u2", "e2", "A2"))

    # The active line's rows are the ORIGINALS, stamps and ids unchanged —
    # its live dsh mapping keeps working.
    a1 = (await test_db.query(
        "SELECT * FROM type::record('messages', $id)", {"id": t.mid("a1")}))[0]
    assert a1["driver_seq"] == 5 and a1["driver_session"] == "L0"
    assert a1["chat_id"] == sid

    # Two carved branches, one per abandoned leaf, keyed by their seed source.
    branches = await _branch_sessions(test_db, sid)
    assert len(branches) == 2
    by_seed = {
        r.get("seed_source_session"): (bid, r)
        for bid, r in branches.items()
    }
    assert set(by_seed) == {"L1", "L2"}
    for seed_src, seed_seq, leaf, fork_row in (
        ("L1", 9, "A1a", "e1a"), ("L2", 11, "A1b", "e1b"),
    ):
        bid, branch = by_seed[seed_src]
        assert branch["forked_from_session"] == sid
        # The fork point: the deepest row shared with the ACTIVE line.
        assert branch["forked_after_origin"] == t.mid("u2")
        assert branch["active_branch_id"] == bid
        assert branch["seed_source_seq"] == seed_seq
        assert branch.get("compacted_from") is None
        assert branch["project_id"] == pid
        assert branch["user_id"] == root["user_id"]

        rows = await _branch_rows(test_db, bid)
        assert [r["origin_id"] for r in rows] == \
            [t.mid(n) for n in ("u1", "a1", "u2", fork_row, leaf)]
        # Copy fidelity: seqs kept (the seed retains parent seqs), the copy's
        # log is the BRANCH's own id, the chain is re-pointed onto the copies.
        by_origin = {r["origin_id"]: r for r in rows}
        assert by_origin[t.mid("a1")]["driver_seq"] == 5
        assert by_origin[t.mid("u1")].get("parent_id") is None
        assert by_origin[t.mid("a1")]["parent_id"] == by_origin[t.mid("u1")]["mid"]
        assert by_origin[t.mid(leaf)]["parent_id"] == by_origin[t.mid(fork_row)]["mid"]
        for r in rows:
            assert r["driver_session"] == bid  # the branch's own log
            assert r["chat_id"] == bid


async def test_flat_tree_forks_offer_each_branch_from_the_fork_point(
    client, admin_user, project_with_doc, test_db,
):
    """/forks on the root names both carved branches at the u2 origin; each
    carved branch sees the root (and its sibling) back."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, t = await _flat_tree(client, token, test_db, pid, doc_id)
    await _migrate_chat_branches_to_sessions(test_db)
    branches = await _branch_sessions(test_db, sid)

    out = await _forks(client, token, sid)
    assert len(out) == 1
    assert out[0]["after_origin"] == t.mid("u2")
    assert sorted(o["session_id"] for o in out[0]["options"]) == sorted([sid, *branches])
    assert [o["session_id"] for o in out[0]["options"] if o["current"]] == [sid]

    for bid in branches:
        mine = await _forks(client, token, bid)
        assert len(mine) == 1
        assert mine[0]["after_origin"] == t.mid("u2")
        # The root is always an option; the other carved branch is too.
        offered = {o["session_id"] for o in mine[0]["options"]}
        assert sid in offered


# ─── the nested fixture: a fork off a non-active branch ─────────────────────


async def test_nested_fork_copies_the_shared_segment_into_both_leaves(
    client, admin_user, project_with_doc, test_db,
):
    """A fork off a NON-active branch: both carved leaves (the outer branch
    and the inner fork) carry the shared segment u1..e1, and /forks on the
    outer branch shows the INNER fork at the e1 origin."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _legacy_chat(client, token, pid, doc_id, test_db)
    t = _Tree(test_db, sid)
    # Creation order == created_at order: the ACTIVE line's tail (A2) must be
    # the NEWEST stamped row — the migration anchors the active line there.
    await t.row("u1", None, "user")
    await t.row("a1", "u1", "assistant", driver_seq=5, driver_session="L0")
    await t.row("u2", "a1", "user")
    await t.row("e1", "u2", "user")
    await t.row("A1", "e1", "assistant", driver_seq=9, driver_session="L1")
    await t.row("f1", "e1", "user")
    await t.row("F1", "f1", "assistant", driver_seq=15, driver_session="L4")
    await t.row("e2", "u2", "user")
    await t.row("A2", "e2", "assistant", driver_seq=13, driver_session="L3")

    await _migrate_chat_branches_to_sessions(test_db)

    # The active line keeps the original; e1's subtree left it entirely.
    assert await t.live_mids() == sorted(t.mid(n) for n in ("u1", "a1", "u2", "e2", "A2"))

    branches = await _branch_sessions(test_db, sid)
    assert len(branches) == 2
    by_tail = {}
    for bid, row in branches.items():
        rows = await _branch_rows(test_db, bid)
        by_tail[rows[-1]["origin_id"]] = (bid, row, rows)
    outer_bid, outer_row, outer_rows = by_tail[t.mid("A1")]
    inner_bid, inner_row, inner_rows = by_tail[t.mid("F1")]

    # Both leaves got the shared root→e1 segment copied.
    assert [r["origin_id"] for r in outer_rows] == \
        [t.mid(n) for n in ("u1", "a1", "u2", "e1", "A1")]
    assert [r["origin_id"] for r in inner_rows] == \
        [t.mid(n) for n in ("u1", "a1", "u2", "e1", "f1", "F1")]
    assert outer_row["seed_source_session"] == "L1"
    assert outer_row["seed_source_seq"] == 9
    assert inner_row["seed_source_session"] == "L4"
    assert inner_row["seed_source_seq"] == 15
    # The fork point is measured against the ACTIVE line for both (u2).
    assert outer_row["forked_after_origin"] == t.mid("u2")
    assert inner_row["forked_after_origin"] == t.mid("u2")

    # /forks on the outer branch shows the INNER fork at the e1 origin.
    mine = await _forks(client, token, outer_bid)
    by_p = {e["after_origin"]: e["options"] for e in mine}
    assert [o["session_id"] for o in by_p[t.mid("e1")] if not o["current"]] == [inner_bid]
    assert by_p[t.mid("u2")][0]["session_id"] == sid


# ─── the unstamped fixture: the 422 marker ───────────────────────────────────


async def test_unstamped_branch_gets_the_marker_not_a_wrong_seed(
    client, admin_user, project_with_doc, test_db,
):
    """A lineage with model history but no row carrying the (session, seq)
    pair (pre-harness / pre-pair stamps) is marked seed_source_session WITHOUT
    a seq: the completions hook answers 422, never a seed from a wrong log."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _legacy_chat(client, token, pid, doc_id, test_db)
    t = _Tree(test_db, sid)
    # Creation order == created_at order: A2 (the active line's tail) last —
    # the newest stamped assistant anchors the active line.
    await t.row("u1", None, "user")
    await t.row("a1", "u1", "assistant")  # pre-harness: no stamps anywhere
    await t.row("u2", "a1", "user")
    await t.row("e1", "u2", "user")
    await t.row("A1", "e1", "assistant", driver_seq=7)  # pre-PAIR: seq, no session
    await t.row("e2", "u2", "user")
    await t.row("A2", "e2", "assistant", driver_seq=13, driver_session="L3")

    await _migrate_chat_branches_to_sessions(test_db)

    branches = await _branch_sessions(test_db, sid)
    assert len(branches) == 1
    branch = next(iter(branches.values()))
    assert branch["seed_source_session"] == sid  # the chat's own dsh id
    assert branch.get("seed_source_seq") is None


# ─── the root-level fixture: two first messages ──────────────────────────────


async def test_two_first_messages_fork_at_the_virtual_root_position(
    client, admin_user, project_with_doc, test_db,
):
    """Legacy root forks made several first messages: the older root lineage
    is carved with forked_after_origin NONE and /forks renders it at the
    virtual P=None position."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _legacy_chat(client, token, pid, doc_id, test_db)
    t = _Tree(test_db, sid)
    await t.row("u1a", None, "user")
    await t.row("a1a", "u1a", "assistant", driver_seq=5, driver_session="L0")
    await t.row("u1b", None, "user")
    await t.row("a1b", "u1b", "assistant", driver_seq=7, driver_session="L0")

    await _migrate_chat_branches_to_sessions(test_db)

    # The newest stamped assistant (a1b) anchors the active line.
    assert await t.live_mids() == sorted(t.mid(n) for n in ("u1b", "a1b"))
    branches = await _branch_sessions(test_db, sid)
    assert len(branches) == 1
    bid, branch = next(iter(branches.items()))
    assert branch.get("forked_after_origin") is None  # shares NOTHING
    assert branch["seed_source_session"] == "L0"
    assert branch["seed_source_seq"] == 5
    rows = await _branch_rows(test_db, bid)
    assert [r["origin_id"] for r in rows] == [t.mid("u1a"), t.mid("a1a")]

    out = await _forks(client, token, sid)
    assert [e["after_origin"] for e in out] == [None]
    assert sorted((o["session_id"], o["next_origin"], o["current"])
                  for o in out[0]["options"]) == sorted([
        (sid, t.mid("u1b"), True), (bid, t.mid("u1a"), False)])


# ─── idempotency + the no-tree paths ─────────────────────────────────────────


async def test_rerun_is_a_noop(
    client, admin_user, project_with_doc, test_db,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid, t = await _flat_tree(client, token, test_db, pid, doc_id)
    await _migrate_chat_branches_to_sessions(test_db)
    before = await _branch_sessions(test_db, sid)
    live_before = await t.live_mids()

    await _migrate_chat_branches_to_sessions(test_db)

    after = await _branch_sessions(test_db, sid)
    assert sorted(after) == sorted(before)
    assert await t.live_mids() == live_before
    # No duplicate copies of any origin either.
    copies = await test_db.query(
        "SELECT VALUE origin_id FROM messages WHERE origin_id IN $mids AND deleted_at IS NONE",
        {"mids": [t.mid("A1a"), t.mid("A1b")]},
    )
    assert sorted(copies) == sorted([t.mid("A1a"), t.mid("A1b")])


async def test_linear_chat_is_threaded_without_carving(
    client, admin_user, project_with_doc, test_db,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _legacy_chat(client, token, pid, doc_id, test_db)
    t = _Tree(test_db, sid)
    await t.row("u1", None, "user")
    await t.row("a1", "u1", "assistant", driver_seq=5, driver_session="L0")
    await t.row("u2", "a1", "user")
    live_before = await t.live_mids()

    await _migrate_chat_branches_to_sessions(test_db)

    root = (await test_db.query(
        "SELECT * FROM type::record('chat_sessions', $id)", {"id": sid}))[0]
    assert root["thread_id"] == sid
    assert root["active_branch_id"] == sid
    assert await _branch_sessions(test_db, sid) == {}
    assert await t.live_mids() == live_before


async def test_note_chats_are_untouched(
    client, admin_user, project_with_doc, test_db,
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _legacy_chat(client, token, pid, doc_id, test_db, is_note=True)
    t = _Tree(test_db, sid)
    await t.row("u1", None, "user")
    await t.row("u1b", None, "user")  # a note tree with two roots, even

    await _migrate_chat_branches_to_sessions(test_db)

    root = (await test_db.query(
        "SELECT * FROM type::record('chat_sessions', $id)", {"id": sid}))[0]
    assert root.get("thread_id") is None
    assert root.get("active_branch_id") is None
    assert len(await t.live_mids()) == 2


def test_registered_in_registry():
    from migrations.runner import _MIGRATIONS

    names = [name for name, _ in _MIGRATIONS]
    assert "chat_branches_to_sessions" in names
