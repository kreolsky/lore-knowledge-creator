"""Chat recall search — the caller's own chats of a project, scoped by document subtree.

# ARCH: the index (chat_chunks, written by jobs.tasks.chat_index) only PROPOSES
# candidates; every answer is re-decided over live chat_sessions + messages rows
# (visible_sessions, live_rows). The caller is the agent key's owning user, the
# project the key's project — neither ever comes from a tool argument.
"""
from datetime import datetime
from typing import Literal, NamedTuple

import settings
from content_hash import hash_content
from fastapi import HTTPException

import embeddings
from db import extract_id, fetch_one, get_ancestor_ids
from jobs.tasks.chat_index import index_coverage
from scope import subtree_doc_ids

ChatScope = Literal["project", "siblings", "subtree"]

CHAT_RECALL_MAX_HITS = 8
# Chunk rows scored before collapsing to messages and origins — headroom for
# branch copies and multi-chunk messages competing for the same hit slots.
CHAT_RECALL_FETCH = 40
CHAT_RECALL_SNIPPET_CHARS = 400
CHAT_RECALL_QUESTION_CHARS = 200

MESSAGE_FIELDS = "meta::id(id) AS id, chat_id, role, content, created_at, parent_id"


class Window(NamedTuple):
    """A message time window `(since, until]`; None leaves that side open."""

    since: datetime | None = None
    until: datetime | None = None


def iso(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


async def _outer_flagged_roots(db, project_id: str, viewer_doc_id: str | None) -> set[str]:
    """Live flagged documents the viewer sits OUTSIDE of.

    V is inside F iff F is among V's ancestors (`get_ancestor_ids` includes V
    itself); V = None is outside every F. Empty when nothing is flagged —
    then no walk is owed at all. `archived` is NOT a filter — archiving a
    room must not open it.
    """
    flagged = await db.query(
        "SELECT meta::id(id) AS did FROM documents "
        "WHERE project_id = $pid AND chats_unindexed = true AND deleted_at IS NONE",
        {"pid": project_id},
    ) or []
    roots = {r["did"] for r in flagged if r.get("did")}
    if not roots:
        return set()
    inside = set(await get_ancestor_ids(viewer_doc_id)) if viewer_doc_id else set()
    return roots - inside


async def _children_map(db, project_id: str) -> dict[str, list[str]]:
    """Live parent→children of the project, from ONE fetch shared across every
    flagged root — never one `get_descendant_ids` project scan per root."""
    rows = await db.query(
        "SELECT meta::id(id) AS did, parent_id FROM documents "
        "WHERE project_id = $pid AND deleted_at IS NONE",
        {"pid": project_id},
    ) or []
    children: dict[str, list[str]] = {}
    for r in rows:
        parent = r.get("parent_id")
        if not parent:
            continue
        pid = extract_id(parent) if not isinstance(parent, str) or ":" in parent else parent
        if pid:
            children.setdefault(pid, []).append(r["did"])
    return children


def _subtree_closure(children: dict[str, list[str]], roots: set[str]) -> set[str]:
    """`roots` plus every descendant under them (BFS)."""
    # WHY no depth cap (unlike get_descendant_ids): a cap here fails OPEN — chats
    # below the cap would leak out of the room. A cyclic parent_id still ends the
    # walk: a node already in `hidden` is never expanded again.
    hidden = set(roots)
    queue = [c for f in roots for c in children.get(f, [])]
    while queue:
        cid = queue.pop()
        if cid in hidden:
            continue
        hidden.add(cid)
        queue.extend(children.get(cid, []))
    return hidden


async def hidden_doc_ids(db, project_id: str, viewer_doc_id: str | None) -> set[str]:
    """Documents whose chats are hidden from this viewer: the union of
    `subtree(F)` over every live flagged document F with the viewer OUTSIDE it.

    # ARCH: read-time, viewer-relative, never denormalized. The flag lives on
    # the document and is never copied onto chats, so a chat reparent or a subtree move changes
    # visibility with no write to any chat. Cost: one indexed query for
    # flagged docs; with any flag, one ancestor walk of V plus at most ONE
    # project-docs fetch shared across all such F.
    """
    outer = await _outer_flagged_roots(db, project_id, viewer_doc_id)
    if not outer:
        return set()
    return _subtree_closure(await _children_map(db, project_id), outer)


async def visible_sessions(
    db, user_id: str, project_id: str, exclude: set[str],
    doc_ids: set[str] | None = None,
    viewer_doc_id: str | None = None,
    include_doc_unindexed: bool = False,
) -> dict[str, dict]:
    """`{session id: {title, root, document_id}}` of the sessions an answer may
    draw from; `doc_ids` keeps only sessions whose root's document is in it.
    `viewer_doc_id` is the calling conversation's root document: a chat whose
    root document sits inside a `chats_unindexed` subtree the viewer is OUTSIDE
    is dropped (include_doc_unindexed=True opts out — digest only)."""
    # INVARIANT(security): what the user deleted, marked unindexed, or put under a
    # hidden subtree is never returned to a viewer outside that subtree.
    # Why: deletion paths are many (session delete, message delete, the
    # document cascade) and the index can lag or fail — so every search and
    # every window read re-checks live rows here and in live_rows; the index
    # only proposes candidates. The subtree hide is decided the same way, at
    # read time against the live tree (hidden_doc_ids), so a chat or a
    # document that moves across the boundary changes visibility with no write
    # to any chat; a caller that forgets the viewer fails SAFE (V = None is
    # outside every flagged subtree).
    rows = await db.query(
        "SELECT meta::id(id) AS sid, title, thread_id, created_at FROM chat_sessions "
        "WHERE user_id = $uid AND project_id = $pid AND deleted_at IS NONE "
        "AND is_note = false AND unindexed = false",
        {"uid": user_id, "pid": project_id},
    ) or []
    sessions = {r["sid"]: {"title": r.get("title") or "", "root": r.get("thread_id") or r["sid"],
                           "created_at": r.get("created_at")}
                for r in rows if r["sid"] not in exclude}
    root_docs = await root_documents(db, {s["root"] for s in sessions.values()})
    for s in sessions.values():
        s["document_id"] = root_docs.get(s["root"])
    if not include_doc_unindexed:
        hidden = await hidden_doc_ids(db, project_id, viewer_doc_id)
        if hidden:
            sessions = {sid: s for sid, s in sessions.items()
                        if s["document_id"] not in hidden}
    if doc_ids is None:
        return sessions
    return {sid: s for sid, s in sessions.items() if s["document_id"] in doc_ids}


async def root_documents(db, root_ids: set[str]) -> dict[str, str | None]:
    """`{root id: document_id}` — a thread's scope document is its ROOT's column.

    WHY the root: a reparent writes document_id on the one row it PATCHes and a
    branch only copies it at creation, so per-row columns disagree across a
    thread after a reparent; every branch is in or out of a scope together. No
    deleted_at filter: the document cascade deletes per row, so a live branch
    can sit under a dead root — which still names the thread's document.
    """
    if not root_ids:
        return {}
    rows = await db.query(
        "SELECT meta::id(id) AS sid, document_id "
        "FROM $ids.map(|$x| type::record('chat_sessions', $x))",
        {"ids": sorted(root_ids)},
    ) or []
    return {r["sid"]: r.get("document_id") for r in rows if r.get("sid")}


async def live_rows(db, message_ids: set[str], sessions: dict) -> dict[str, dict]:
    """The live messages among `message_ids` that sit in a visible session."""
    if not message_ids or not sessions:
        return {}
    rows = await db.query(
        f"SELECT {MESSAGE_FIELDS} "
        "FROM $mids.map(|$x| type::record('messages', $x)) "
        "WHERE deleted_at IS NONE AND chat_id IN $sids",
        {"mids": sorted(message_ids), "sids": sorted(sessions)},
    ) or []
    return {r["id"]: r for r in rows}


async def require_scope_document(project_id: str, doc_id: str) -> None:
    """Unknown / deleted / other-project document → uniform 404 (no existence oracle)."""
    row = await fetch_one("documents", doc_id)
    if not row or row.get("deleted_at") or row.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="Search root document not found")


# ─── search ──────────────────────────────────────────────────────────────────


async def _embed_query(query: str) -> list[float]:
    """The query vector — asymmetric model, so the query takes the retrieval
    instruction (see the INVARIANT of embed_texts). Failure is an error, never
    an empty result that would read as "never said"."""
    try:
        vecs = await embeddings.embed_texts(
            [query], instruction=await settings.get("RETRIEVAL_QUERY_INSTRUCTION"))
    except embeddings.EmbeddingConfigError as e:
        raise HTTPException(status_code=503, detail="embeddings not configured") from e
    except Exception as e:
        raise HTTPException(status_code=503, detail="embedding failed") from e
    return vecs[0]


async def _score_chunks(
    db, user_id: str, project_id: str, sessions: dict, qvec: list[float],
    window: Window,
) -> list[dict]:
    """Top chunks by cosine over the visible sessions, above the similarity floor.

    WHY model + dimension guard: the embedding model is a deploy variable — a
    chunk of another model is noise against this query, and a chunk of another
    dimension makes vector::similarity::cosine raise.
    """
    if not sessions:
        return []
    rows = await db.query(
        "SELECT message_id, origin_key, content, content_hash, "
        "vector::similarity::cosine(embedding, $q) AS score FROM chat_chunks "
        "WHERE user_id = $uid AND project_id = $pid AND chat_id IN $sids "
        "AND model = $model AND array::len(embedding) = $dim "
        "AND ($since IS NONE OR created_at > $since) "
        "AND ($until IS NONE OR created_at <= $until) "
        "ORDER BY score DESC LIMIT $n",
        {"q": qvec, "uid": user_id, "pid": project_id, "sids": sorted(sessions),
         "model": await settings.get("EMBEDDING_MODEL"), "dim": len(qvec),
         "since": window.since, "until": window.until, "n": CHAT_RECALL_FETCH},
    ) or []
    floor = await settings.get("RETRIEVAL_MIN_SCORE")
    return [r for r in rows if r["score"] >= floor]


def _collapse(chunks: list[dict], live: dict[str, dict]) -> list[dict]:
    """Best chunk per message, then per origin (a branch copy is the same row),
    capped at CHAT_RECALL_MAX_HITS. A chunk whose message changed since it was
    indexed is dropped — its text is no longer what the message says."""
    picked: list[dict] = []
    seen: set[str] = set()
    for chunk in chunks:
        message = live.get(chunk["message_id"])
        if not message or chunk["content_hash"] != hash_content(message["content"]):
            continue
        if chunk["origin_key"] in seen:
            continue
        seen.add(chunk["origin_key"])
        picked.append({**chunk, "message": message})
    return picked[:CHAT_RECALL_MAX_HITS]


async def _ranked(
    db, user_id: str, project_id: str, sessions: dict, qvec: list[float], window: Window,
) -> list[dict]:
    """The picked chunks, by similarity alone.

    WHY no boost for the anchor's subtree: a chat on the open document is MARKED
    (`sibling`), never ranked above a more relevant one — the narrow to that
    subtree is the explicit `siblings` scope.
    """
    chunks = await _score_chunks(db, user_id, project_id, sessions, qvec, window)
    live = await live_rows(db, {c["message_id"] for c in chunks}, sessions)
    return _collapse(chunks, live)


async def _scoped_sessions(
    db, user_id: str, project_id: str, exclude: set[str], scope: ChatScope,
    siblings: set[str] | None, under_document_id: str | None,
    viewer_doc_id: str | None = None, include_doc_unindexed: bool = False,
) -> dict:
    if scope == "siblings":
        if siblings is None:
            raise HTTPException(
                status_code=400,
                detail="no anchor document — use scope=project or subtree")
        return await visible_sessions(db, user_id, project_id, exclude, siblings,
                                      viewer_doc_id, include_doc_unindexed)
    if scope == "subtree":
        await require_scope_document(project_id, under_document_id)
        under = set(await subtree_doc_ids(under_document_id, project_id))
        return await visible_sessions(db, user_id, project_id, exclude, under,
                                      viewer_doc_id, include_doc_unindexed)
    return await visible_sessions(db, user_id, project_id, exclude,
                                  viewer_doc_id=viewer_doc_id,
                                  include_doc_unindexed=include_doc_unindexed)


async def search_chats(
    db, user_id: str, project_id: str, query: str, *, exclude: set[str],
    scope: ChatScope, anchor_doc_id: str | None, under_document_id: str | None,
    window: Window = Window(),
    viewer_doc_id: str | None = None, include_doc_unindexed: bool = False,
) -> dict:
    """Semantic search over the user's own visible chats of the project.

    `project` searches everything (hits under the anchor are marked `sibling`); `siblings`
    only the anchor document and its subtree (400 without an anchor — a narrow
    the caller cannot get is an error, never a wider search); `subtree` only
    under `under_document_id` (404 when it is not a live document of the project).
    `window` keeps only messages created inside it. `viewer_doc_id` is the
    caller's VIEWER document — a chat inside a `chats_unindexed` subtree the
    viewer is outside is never returned (see visible_sessions); default None
    fails safe (outside every flagged subtree).
    """
    siblings = (set(await subtree_doc_ids(anchor_doc_id, project_id))
                if anchor_doc_id else None)
    sessions = await _scoped_sessions(
        db, user_id, project_id, exclude, scope, siblings, under_document_id,
        viewer_doc_id, include_doc_unindexed)
    qvec = await _embed_query(query)
    picked = await _ranked(db, user_id, project_id, sessions, qvec, window)
    parents = await live_rows(db, {
        p["message"]["parent_id"] for p in picked
        if p["message"]["role"] == "assistant" and p["message"].get("parent_id")
    }, sessions)
    hits = [_hit(p, sessions, parents, siblings or set()) for p in picked]
    result: dict = {"hits": hits}
    # WHY only_chat_ids for EVERY scope: `indexed: N of M` must count what THIS
    # caller can see — for scope=project `sessions` is already the full visible
    # set, so an unfiltered count would report chats the subtree hide removed.
    indexed, total = await index_coverage(
        db, user_id, project_id, await settings.get("EMBEDDING_MODEL"), exclude,
        only_chat_ids=set(sessions))
    if indexed < total:
        result["indexed"] = f"{indexed} of {total}"
    return result


def _hit(picked: dict, sessions: dict, parents: dict[str, dict], siblings: set[str]) -> dict:
    message = picked["message"]
    session = sessions[message["chat_id"]]
    hit = {
        "chat_id": message["chat_id"], "thread_id": session["root"],
        "chat_title": session["title"], "document_id": session["document_id"],
        "message_id": message["id"], "role": message["role"],
        "created_at": iso(message.get("created_at")),
        "snippet": picked["content"][:CHAT_RECALL_SNIPPET_CHARS],
        "score": round(picked["score"], 3),
        "sibling": session["document_id"] in siblings,
    }
    parent = parents.get(message.get("parent_id") or "")
    if message["role"] == "assistant" and parent and parent["role"] == "user":
        hit["question"] = parent["content"][:CHAT_RECALL_QUESTION_CHARS]
    return hit
