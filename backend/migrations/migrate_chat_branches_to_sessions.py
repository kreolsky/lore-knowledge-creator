"""Migration: chat_branches_to_sessions — carve legacy message trees into
branch sessions.

Before: an AI chat was ONE chat_sessions row whose messages formed a tree
(parent_id siblings) while the dsh log held only ONE lineage — the active
one. Abandoned branches rendered text-only on reload, and continuing one
re-pointed the chat's dsh mapping on every fork (the leaf-move seam). After:
a branch is its OWN session row bound for life to ONE dsh session whose id
equals the Lore session id. This migration carves every non-active leaf
lineage out as a branch session holding a COPY of its whole root→leaf chain
(origins preserved), stamps the lazy-seed pair its first completion feeds to
the plugin's /session-fork, soft-deletes the abandoned rows from the
original, and self-threads every legacy AI chat (thread_id =
active_branch_id = own id).

# ARCH: the migration NEVER calls the driver — it runs at backend boot, where
# the harness may be down. The active line is therefore picked WITHOUT a
# replay, the way the reload's retired stamp-chain walk used to anchor it: the newest
# assistant row carrying `driver_seq` (the turn lock serialized turns, so
# completion order = row order), else the newest leaf. A carved branch
# renders text-only until its first turn seeds its log — exactly the state
# abandoned branches had.

# ARCH: the active lineage's rows, the original session's id, and its
# `compacted_from` are never touched — the live dsh mapping (SessionMap /
# identity) keeps resolving for the thread root after the migration.

# INVARIANT(corruption): the seed pair is taken off a row that carries BOTH
# `driver_seq` AND `driver_session`. Why: a seq resolves only inside its own
# session's log, and for a row on an ABANDONED lineage the chat's current
# dsh id (the /branches fallback for pre-pair rows) names the WRONG log —
# seeding from it would either 422 at the plugin's boundary guard or, worse,
# seed a coincidental `turn/end` from another branch's history. A lineage
# with model history but no row carrying the pair (pre-harness, pre-pair
# stamps) is stamped `seed_source_session` WITHOUT a seq — the marker the
# completions hook answers with 422 "branch point not resolvable" (today's
# answer), never a silently empty model history. A lineage with no assistant
# row at all has nothing to seed and gets no stamp (a fresh log, the
# /branches user-only ruling).

Idempotent: chats already threaded (thread_id set) are skipped, and a run
interrupted mid-carve re-runs safely — a leaf whose copy already exists is
detected by that copy's origin_id (only the leaf's own carved branch can
carry a leaf's origin), and the original is marked threaded only once fully
processed.
"""

from __future__ import annotations

from uuid import uuid4

from db import create_record, extract_id
from migrations._shared import logger

# WHY (patch seams, the branches.py precedent): _origin_of/_COPY_FIELDS/
# _copy_prefix/_lineage_around below are verbatim twins of
# routes.chat.branches and of the retired reload walk — a shared import would
# couple a boot-critical migration to private route internals (the
# module-boundary gate) and to the churn of those modules; the twins die with this
# migration when it retires.

# The content-bearing columns a prefix copy preserves verbatim — driver_seq
# included: the seed retains the parent prefix seqs, so the branch's log
# addresses its copied turns by the SAME seqs (the switcher and any later
# fork of the branch read the pair off the copy). NOT copied: id/chat_id/
# parent_id (new row, new chat, re-pointed chain), driver_session (the copy
# runs in the BRANCH's own log), origin_id (the SOURCE's origin),
# deleted_at (a soft-deleted row breaks the chain before the copy).
_COPY_FIELDS = (
    "role", "content", "images", "model", "created_at", "author_id",
    "sources", "halt", "gen_steps", "agent_trace", "driver_seq",
)

# The per-session config a carved branch copies from its original — the same
# set `branches._branch_session_data` copies, for the same reasons. NOT
# copied: `compacted_from` (that column names the dsh log a chat's turns run
# in; the branch's log is its own id) and per-conversation UI state
# (unread_for, anchors, title_user_set).
_OPTIONAL_SESSION_FIELDS = (
    "document_id", "system_prompt_id", "reasoning_effort",
    "context_document_ids",
)


def _origin_of(row: dict) -> str:
    """The row's origin — its source row's identity across every copy."""
    return row.get("origin_id") or str(extract_id(row.get("id")) or "")


async def _copy_prefix(new_id: str, chain: list[dict]) -> None:
    """Copy the prefix root→leaf as NEW rows, re-pointing the chain onto the
    copies. The copies go stale on purpose: editing a prefix row in one
    branch never changes another (no propagation)."""
    prev: str | None = None
    for src in chain:
        copy = {k: src[k] for k in _COPY_FIELDS if src.get(k) is not None}
        copy["chat_id"] = new_id
        copy["parent_id"] = prev
        copy["driver_session"] = new_id
        copy["origin_id"] = _origin_of(src)
        prev = str(uuid4())
        await create_record("messages", prev, copy)


def _lineage_around(head_mid: str, by_mid: dict[str, dict]) -> set[str]:
    """The head's ancestors plus the single forward chain under it (latest
    child per node) — the lineage the log holds."""
    chain: set[str] = set()
    cursor: str | None = head_mid
    while cursor and cursor in by_mid and cursor not in chain:
        chain.add(cursor)
        cursor = by_mid[cursor].get("parent_id")
    cursor = head_mid
    while True:
        kids = [
            m for m, r in by_mid.items()
            if r.get("parent_id") == cursor
        ]
        if not kids:
            break
        nxt = max(kids, key=lambda k: by_mid[k].get("created_at") or "")
        if nxt in chain:
            break
        chain.add(nxt)
        cursor = nxt
    return chain


def _newest(rows: list[dict]) -> dict:
    # created_at is DEFAULT time::now() — always present; the mid tie-break
    # keeps the pick deterministic on identical timestamps.
    return max(rows, key=lambda r: (r.get("created_at") or "", r["mid"]))


def _has_fork(light_rows: list[dict]) -> bool:
    """Any live sibling anywhere: two live rows under one effective parent.
    A parent outside the live set counts as the virtual root — the /forks
    None-P normalization (a dead parent's children are root siblings)."""
    mids = {r["mid"] for r in light_rows}
    counts: dict[object, int] = {}
    for r in light_rows:
        pid = r.get("parent_id")
        key = pid if pid in mids else None
        counts[key] = counts.get(key, 0) + 1
    return any(c > 1 for c in counts.values())


def _seed_pair(lineage: list[dict]) -> tuple[str, int] | None:
    """The lazy-seed stamp: the DEEPEST row carrying both halves of the
    (session, seq) pair. A seed from that boundary retains the whole prefix
    — the old fork seeds kept parent seqs, so the branch's own log addresses
    its copied turns by the SAME seqs (module INVARIANT on the pair)."""
    for r in reversed(lineage):
        seq = r.get("driver_seq")
        if (
            isinstance(seq, int) and not isinstance(seq, bool)
            and r.get("driver_session")
        ):
            return r["driver_session"], seq
    return None


def _branch_data(
    chat: dict, sid: str, lineage: list[dict], new_id: str,
    active_chain: set[str],
) -> dict:
    """The carved branch's chat_sessions row: copied config + thread
    provenance + the lazy-seed stamp (or the 422 marker)."""
    # The fork point: the origin of the DEEPEST row the
    # branch shares with the active line — NONE only for a root-level
    # sibling of a legacy chat, rendered by /forks as the virtual P=None.
    shared = [r for r in lineage if r["mid"] in active_chain]
    data: dict = {
        "project_id": chat["project_id"],
        "user_id": chat["user_id"],
        "title": chat.get("title") or "",
        "model": chat.get("model") or "",
        "target_doc_id": chat.get("target_doc_id"),
        "agent_auto": bool(chat.get("agent_auto")),
        "has_region": bool(chat.get("has_region")),
        "thread_id": sid,
        "forked_from_session": sid,
        "forked_after_origin": _origin_of(shared[-1]) if shared else None,
        "active_branch_id": new_id,
    }
    for optional in _OPTIONAL_SESSION_FIELDS:
        if chat.get(optional) is not None:
            data[optional] = chat[optional]
    pair = _seed_pair(lineage)
    if pair is not None:
        data["seed_source_session"], data["seed_source_seq"] = pair
    elif any(r.get("role") == "assistant" for r in lineage):
        # The 422 marker (module INVARIANT): model history exists but no row
        # names a boundary for it. The value is provenance/diagnostics only —
        # the hook never seeds from a session without a seq.
        data["seed_source_session"] = chat.get("compacted_from") or sid
    if chat.get("deleted_at") is not None:
        # A deleted conversation's branches are born deleted — carving must
        # never resurrect content the operator removed.
        data["deleted_at"] = chat["deleted_at"]
    return data


async def _carve_branch(
    db, chat: dict, sid: str, by_mid: dict[str, dict],
    leaf: dict, active_chain: set[str],
) -> str:
    """Carve one non-active leaf into its own branch session: the leaf's
    whole root→leaf lineage, copied with origins preserved."""
    lineage: list[dict] = []
    seen: set[str] = set()
    cursor: str | None = leaf["mid"]
    while cursor and cursor in by_mid and cursor not in seen:
        seen.add(cursor)
        lineage.append(by_mid[cursor])
        pid = by_mid[cursor].get("parent_id")
        cursor = pid if pid in by_mid else None
    lineage.reverse()  # root→leaf — _copy_prefix's order

    new_id = str(uuid4())
    await create_record(
        "chat_sessions", new_id,
        _branch_data(chat, sid, lineage, new_id, active_chain),
    )
    await _copy_prefix(new_id, lineage)
    return new_id


def _non_active_leaves(rows: list[dict]) -> tuple[dict[str, dict], list[dict], set[str]]:
    """(by_mid, non-active leaves, the active line's mids) over one live tree.

    The active line is picked WITHOUT a replay (module ARCH): the newest
    assistant row carrying `driver_seq`, else the newest leaf; the line is
    its parent chain plus the single forward chain under it
    (`_lineage_around`, the retired reload walk's twin)."""
    by_mid = {r["mid"]: r for r in rows}
    parented = {r.get("parent_id") for r in rows if r.get("parent_id")}
    leaves = [r for r in rows if r["mid"] not in parented]
    stamped = [
        r for r in rows
        if r.get("role") == "assistant"
        and isinstance(r.get("driver_seq"), int)
        and not isinstance(r.get("driver_seq"), bool)
    ]
    head = _newest(stamped) if stamped else _newest(leaves or rows)
    active_chain = _lineage_around(head["mid"], by_mid)
    return by_mid, [r for r in leaves if r["mid"] not in active_chain], active_chain


async def _carve_tree(db, chat: dict, sid: str) -> int:
    """Carve every non-active leaf of one legacy tree; soft-delete the rows
    that leave the original. Returns the number of branches carved."""
    rows = await db.query(
        "SELECT meta::id(id) AS mid, * FROM messages "
        "WHERE chat_id = $cid AND deleted_at IS NONE",
        {"cid": sid},
    ) or []
    if not rows:
        return 0
    by_mid, strays, active_chain = _non_active_leaves(rows)
    if not strays:
        return 0
    # Idempotent carve (module docstring): a leaf already copied is skipped
    # by its copy's origin_id.
    already = await db.query(
        "SELECT VALUE origin_id FROM messages "
        "WHERE origin_id IN $mids AND deleted_at IS NONE",
        {"mids": [r["mid"] for r in strays]},
    ) or []
    carved_origins = {str(o) for o in already}
    strays = [r for r in strays if r["mid"] not in carved_origins]
    carved = 0
    for leaf in strays:
        await _carve_branch(db, chat, sid, by_mid, leaf, active_chain)
        carved += 1
    # The original keeps ONLY its active line — everything else now lives as
    # copies in the carved branches.
    await db.query(
        "UPDATE messages SET deleted_at = time::now() "
        "WHERE chat_id = $cid AND meta::id(id) NOT IN $keep "
        "AND deleted_at IS NONE",
        {"cid": sid, "keep": sorted(active_chain)},
    )
    logger.info(
        "chat_branches_to_sessions: carved %d branch(es) out of %s "
        "(%d live rows -> %d on the active line)",
        carved, sid, len(rows), len(active_chain),
    )
    return carved


async def _migrate_chat_branches_to_sessions(db) -> None:
    """Thread every legacy AI chat; carve the non-active leaves of its tree
    out as branch sessions."""
    chats = await db.query(
        "SELECT meta::id(id) AS sid, * FROM chat_sessions "
        "WHERE thread_id IS NONE AND (is_note IS NONE OR is_note = false)",
    ) or []
    if not chats:
        return
    # Structure-only pass in ONE query: sibling detection needs no content.
    sids = [c["sid"] for c in chats]
    light = await db.query(
        "SELECT chat_id, meta::id(id) AS mid, parent_id FROM messages "
        "WHERE chat_id IN $sids AND deleted_at IS NONE",
        {"sids": sids},
    ) or []
    by_chat: dict[str, list[dict]] = {}
    for r in light:
        by_chat.setdefault(r["chat_id"], []).append(r)
    for chat in chats:
        sid = chat["sid"]
        if _has_fork(by_chat.get(sid) or []):
            await _carve_tree(db, chat, sid)
        # Threaded LAST — the chat is marked done only when fully processed
        # (the idempotent-resume contract in the module docstring).
        await db.query(
            "UPDATE type::record('chat_sessions', $id) "
            "SET thread_id = $id, active_branch_id = $id",
            {"id": sid},
        )
