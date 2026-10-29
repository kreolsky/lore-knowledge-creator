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
