"""Branch-as-session primitives: create a branch (copy the prefix) + the
switcher projection.

A branch is its own
chat_sessions row bound for life to ONE dsh session whose id EQUALS the Lore
session id: forking COPIES the shared prefix rows (origins preserved, parent
links re-pointed onto the copies) and stamps the lazy-seed pair the branch's
first completion feeds to the plugin's /session-fork. Nothing here talks to
the driver.
See SYSTEM: chat-sessions (entry: sessions.py).

# INVARIANT (patch seams): _branch_point below resolves fetch_one in THIS
# module's globals — tests patch THIS module's seam, not a shared import's.
# Why: the walk's read is this surface's own dependency; a shared import
# would couple it to a module the tests do not patch.
"""
import logging
from datetime import datetime
from uuid import uuid4

from chat_sessions.serialize import build_session_ref_map, serialize_session
from fastapi import Depends, HTTPException
from surrealdb import AsyncSurreal

from auth import get_current_user
from db import create_record, extract_id, fetch_one, get_db
from models import BranchCreate
from routes.chat._router import router
from routes.chat.sessions import _require_session_access

logger = logging.getLogger(__name__)


async def _branch_point(parent_id: str) -> tuple[bool, int | None, str | None]:
    """Resolve the fork branch point to the DRIVER's own id — the dsh log seq
    of the nearest ancestor row's turn/end (`messages.driver_seq`) and the dsh
    session that log belongs to (`messages.driver_session`).

    Returns ``(resolvable, seq, source)``: ``seq`` is the boundary the seed
    covers (``None`` = the root branch — the chain never completed a turn);
    ``source`` is the dsh session stamped on the SAME row (``None`` on a row
    stamped before the pair); ``resolvable`` False means the projection
    cannot name a driver boundary and the fork must 422 honestly.

    # ARCH: walking PAST an unstamped row is deliberate. A user row never ends
    # a turn (no stamp of its own), and an abnormally ended turn wrote no
    # `turn/end` — both resolve to the last COMPLETED boundary below them,
    # which is exactly the prefix the seeded fork must cover. No counting:
    # the row's own stamp is the id.
    """
    saw_unaccounted_turn = False
    mid: str | None = parent_id
    seen: set[str] = set()
    while mid:
        if mid in seen:
            return False, None, None  # cycle guard — a corrupt tree is not a branch point
        seen.add(mid)
        row = await fetch_one("messages", mid)
        if row is None:
            return False, None, None
        seq = row.get("driver_seq")
        if seq is not None:
            # INVARIANT(corruption): seq and source come off the SAME row.
            # Why: a seq resolves only inside its own session's log; pairing
            # it with another row's session seeds the fork from the wrong branch.
            return True, int(seq), row.get("driver_session") or None
        if row.get("role") == "assistant":
            saw_unaccounted_turn = True
        mid = row.get("parent_id")
    return (not saw_unaccounted_turn), None, None


async def _prefix_chain(session_id: str, after_message_id: str) -> list[dict]:
    """The root→after_message_id rows (inclusive) as a copy source.

    422 on anything but a live in-session chain: a missing/soft-deleted link
    or a row of another chat is not a branch point (the same honest refusal
    the walk gives), and a parent cycle is a corrupt tree, not a prefix.
    """
    chain: list[dict] = []
    seen: set[str] = set()
    mid: str | None = after_message_id
    while mid:
        if mid in seen:
            raise HTTPException(status_code=422, detail="branch point not resolvable")
        seen.add(mid)
        row = await fetch_one("messages", mid)
        if row is None or row.get("chat_id") != session_id:
            raise HTTPException(status_code=422, detail="branch point not resolvable")
        chain.append(row)
        mid = row.get("parent_id")
    chain.reverse()
    return chain


def _origin_of(row: dict) -> str:
    """The row's origin — its source row's identity across every copy."""
    return row.get("origin_id") or str(extract_id(row.get("id")) or "")


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


def _branch_session_data(session: dict, session_id: str, chain: list[dict],
                         new_id: str, seq: int | None, source: str | None) -> dict:
    """The branch session row: the copied config + thread provenance + the
    lazy-seed stamp. The dsh session is NOT seeded at fork time — the branch's
    first completion feeds the stamp to /session-fork before its turn."""
    data: dict = {
        "project_id": session["project_id"],
        "user_id": session["user_id"],
        "title": session.get("title") or "",
        "model": session.get("model") or "",
        # The agent pin + selector state ride with the branch; the vestigial
        # `mode` column is read by nothing and deliberately not copied.
        "target_doc_id": session.get("target_doc_id"),
        "agent_auto": bool(session.get("agent_auto")),
        "has_region": bool(session.get("has_region")),
        # Thread identity: the branch joins the source's thread (a
        # pre-migration source IS its own thread) and self-anchors its
        # active_branch_id — only the ROOT row's field is ever read.
        "thread_id": session.get("thread_id") or session_id,
        "forked_from_session": session_id,
        "forked_after_origin": _origin_of(chain[-1]),
        "active_branch_id": new_id,
    }
    for optional in ("document_id", "system_prompt_id", "reasoning_effort",
                     "context_document_ids"):
        if session.get(optional) is not None:
            data[optional] = session[optional]
    # INVARIANT(corruption): compacted_from is NOT copied.
    # Why: that column names the dsh log a chat's turns run in — a copy would
    # run the branch's turns inside the SOURCE's log; the branch's log is its
    # own id.
    if seq is not None:
        # Same-row pair off the walk.
        data.update(_seed_stamp(session, session_id, seq, source))
    return data


def _seed_stamp(session: dict, session_id: str, seq: int,
                source: str | None) -> dict:
    """The lazy-seed pair for a branch forked at `seq`. The fallback (a row
    stamped before the pair) names the chat's own dsh id — the id the
    plugin's SessionMap resolves for legacy chats."""
    source = source or (session.get("compacted_from") or session_id)
    # INVARIANT(corruption): forking an UNSEEDED branch seeds from ITS source.
    # Why: its copied rows name the branch (no log yet — a rewind not
    # followed by a send) while their seqs live in the branch's seed source;
    # a fork off the empty id is unresolvable and could never run.
    if source == session_id and session.get("seed_source_session"):
        # A half-stamped source (the migration's 422 marker) passes the
        # marker on: no seq ⇒ the new branch answers 422 honestly too.
        if session.get("seed_source_seq") is None:
            return {"seed_source_session": session["seed_source_session"]}
        return {"seed_source_session": session["seed_source_session"],
                "seed_source_seq": seq}
    return {"seed_source_session": source, "seed_source_seq": seq}


async def _copy_prefix(new_id: str, chain: list[dict]) -> None:
    """Copy the prefix root→leaf as NEW rows, re-pointing the chain onto the
    copies. The copies go stale on purpose: editing a prefix row in one branch
    never changes another (no propagation)."""
    prev: str | None = None
    for src in chain:
        copy = {k: src[k] for k in _COPY_FIELDS if src.get(k) is not None}
        copy["chat_id"] = new_id
        copy["parent_id"] = prev
        copy["driver_session"] = new_id
        copy["origin_id"] = _origin_of(src)
        prev = str(uuid4())
        await create_record("messages", prev, copy)


@router.post("/sessions/{session_id}/branches", status_code=201)
async def create_branch(
    session_id: str,
    body: BranchCreate,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Fork a branch session after a message: a new chat_sessions row of the
    same thread holding a COPY of the shared prefix (rows up to and including
    after_message_id along its parent chain).

    A prefix with no completed turn (the walk's root branch) has nothing to
    seed: no seed_source_* stamp is written and the branch's dsh log starts
    fresh under its own id on the first turn.
    """
    session = await _require_session_access(session_id, user)
    if session.get("is_note") is True:
        raise HTTPException(
            status_code=400,
            detail="note sessions keep their reply tree — branches are AI chats only",
        )
    if not body.after_message_id:
        raise HTTPException(
            status_code=400,
            detail="after_message_id is required — the first message of an AI chat is immutable",
        )
    chain = await _prefix_chain(session_id, body.after_message_id)
    resolvable, seq, source = await _branch_point(body.after_message_id)
    if not resolvable:
        raise HTTPException(status_code=422, detail="branch point not resolvable")

    new_id = str(uuid4())
    data = _branch_session_data(session, session_id, chain, new_id, seq, source)
    row = await create_record("chat_sessions", new_id, data)
    await _copy_prefix(new_id, chain)

    return serialize_session(
        row, await build_session_ref_map(db, data), viewer_id=user["user_id"],
    )


def _thread_continuations(rows: list[dict], created_at: dict[str, str],
                          session_id: str) -> list[dict]:
    """The switcher projection over one thread's rows: for every origin P on
    this branch (None = the virtual "before the first message" position,
    rendered only for migrated legacy chats with several first messages), the
    DISTINCT continuations after P across the branches that contain P — one
    option per next origin, THIS session's own included and flagged
    `current`. Only a P with two or more continuations is a fork.

    WHY: options are distinct NEXT ORIGINS, never sessions — a fork off a
    fork shares its parent's continuation at the outer fork point, and
    counting sessions there shows "1/3" for two different messages.

    Option choice and order: _fork_options.

    The rows carry `mid` (the projection's meta::id alias), NOT the raw `id`
    — a row's origin is its origin_id or that mid. (SurrealDB also DROPS a
    field whose CONTENT value is null, so an absent parent_id IS the root.)"""
    def origin(r: dict) -> str:
        return r.get("origin_id") or str(r["mid"])

    by_key = {(r["chat_id"], str(r["mid"])): r for r in rows}
    sids = list(created_at)
    contains: dict[str, set] = {sid: set() for sid in sids}
    # (session, P) -> the origin the session continues with after P. A root
    # row (no parent) continues after the virtual P = None.
    next_at: dict[tuple[str, str | None], str] = {}
    for r in rows:
        contains[r["chat_id"]].add(origin(r))
        pid = r.get("parent_id")
        parent = by_key.get((r["chat_id"], pid)) if pid else None
        next_at[(r["chat_id"], origin(parent) if parent else None)] = origin(r)

    mine = sorted(
        (r for r in rows if r["chat_id"] == session_id),
        key=lambda r: (r.get("created_at") or "", str(r["mid"])),
    )
    out: list[dict] = []
    for p in [None] + [origin(r) for r in mine]:
        groups: dict[str | None, list[str]] = {}
        for sid in sids:
            if p is not None and p not in contains[sid]:
                continue
            groups.setdefault(next_at.get((sid, p)), []).append(sid)
        if len(groups) >= 2:
            out.append({"after_origin": p,
                        "options": _fork_options(groups, created_at, session_id)})
    return out


def _fork_options(groups: dict[str | None, list[str]],
                  created_at: dict[str, str], session_id: str) -> list[dict]:
    """One option per continuation: this session when it continues that way,
    else the newest session that does; ordered by the earliest session
    continuing each way (the same order from every branch)."""
    options = []
    for q, members in groups.items():
        members.sort(key=lambda sid: (created_at.get(sid) or "", sid))
        current = session_id in members
        options.append({
            "session_id": session_id if current else members[-1],
            "next_origin": q,
            "created_at": created_at.get(members[0]) or None,
            "current": current,
        })
    options.sort(key=lambda o: (o["created_at"] or "", o["session_id"]))
    return options


@router.get("/sessions/{session_id}/forks")
async def list_forks(
    session_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """List the fork options of this branch (the "1/N ◄►" switcher's data)."""
    session = await _require_session_access(session_id, user)
    if session.get("is_note") is True:
        raise HTTPException(
            status_code=400,
            detail="note sessions keep their reply tree — branches are AI chats only",
        )
    thread_id = session.get("thread_id") or session_id
    sess_rows = await db.query(
        "SELECT meta::id(id) AS id, created_at FROM chat_sessions "
        "WHERE (thread_id = $tid OR (thread_id IS NONE AND meta::id(id) = $tid)) "
        "AND deleted_at IS NONE",
        {"tid": thread_id},
        site="chat",
    ) or []
    if not sess_rows:
        return []

    def _iso(created: object) -> str:
        return created.isoformat() if isinstance(created, datetime) else ""

    created_at = {str(r["id"]): _iso(r.get("created_at")) for r in sess_rows}
    rows = await db.query(
        "SELECT meta::id(id) AS mid, chat_id, parent_id, origin_id, created_at "
        "FROM messages WHERE chat_id IN $sids AND deleted_at IS NONE",
        {"sids": list(created_at)},
        site="chat",
    ) or []
    return _thread_continuations(rows, created_at, session_id)
