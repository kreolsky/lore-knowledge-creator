"""Typed-edges read service — ONE graph answer, both directions, every kind.

# SYSTEM: edges — the typed graph read serving /edges and the get_links agent tool

Every relation the product shows as a link answers through ONE endpoint in ONE
shape (plan typed-edges). Stored kinds (`mention`, `transclusion`) are read from
doc_mentions (kind stamped by the rebuild in mentions.py); field-backed kinds are
PROJECTED here at read time from the indexed field that already has many writers
— storing them would duplicate a field and a missed writer would drift the graph
silently:

  - `attachment` (host → material): documents.parent_id with is_reference=true
    (idx_documents_parent_only);
  - `note` (note → document): chat_sessions.document_id with is_note=true
    (idx_chatsessions_document). DIRECT anchor only — the branch-scope walk of
    _note_scope_ids is a listing convenience, not a relation (operator Ruling:
    projecting it would show a note on reference R2 as linked to sibling R1).
    Notes carry no user_id filter: every reader of the project sees them.
  - `chat-context` (chat → document): chat_sessions.context_document_ids, the
    viewer's OWN chats only (idx_chatsessions_user_project).
  - `external` (document → URL): `[t](https://…)` links parsed from the GFM
    read-model — the live session's expand_tables when it has clients, else
    documents.content (the exact branch get_document_links uses). Opt-in: a
    URL is not a graph node, and the agent get_links default output must not
    change. Entries carry url + label instead of document_id.

The REST route (GET /api/documents/{id}/edges) and the agent tool (get_links)
both call collect_edges — one function, one default kind set. Callers gate the
ANCHOR (require_document_read / the agent read gates); this module walls the
PEERS.
"""
from __future__ import annotations

from chat_sessions.previews import PREVIEW_CONTENT_MAX, note_previews
from edge_kinds import (
    ALL_EDGE_KINDS,
    DEFAULT_EDGE_KINDS,
    KIND_ATTACHMENT,
    KIND_CHAT_CONTEXT,
    KIND_EXTERNAL,
    KIND_MENTION,
    KIND_NOTE,
    STORED_EDGE_KINDS,
)
from fastapi import HTTPException

from db import extract_id, fetch_many, fetch_one, get_db
from mentions import extract_external_links
from scope import subtree_doc_ids

# One default + one full vocabulary, canonical in edge_kinds (the leaf every
# kind list derives from — see edge_kinds.py); re-exported here for the
# route/tool/test import surface.
__all__ = [
    "ALL_EDGE_KINDS", "DEFAULT_EDGE_KINDS", "KIND_ATTACHMENT", "KIND_NOTE",
    "KIND_CHAT_CONTEXT", "KIND_EXTERNAL", "collect_edges", "parse_edge_kinds",
]


def parse_edge_kinds(raw: str | None) -> list[str]:
    """Comma-separated `kinds` query value → validated ordered list.

    Empty/None → the one default set. Unknown kind → 422 (a self-correctable
    error, never a silent fallback — same policy as ToolSearch.mode).
    """
    if raw is None or not raw.strip():
        return list(DEFAULT_EDGE_KINDS)
    kinds: list[str] = []
    for part in raw.split(","):
        kind = part.strip()
        if not kind:
            continue
        if kind not in ALL_EDGE_KINDS:
            raise HTTPException(status_code=422, detail=f"Unknown edge kind: {kind!r}")
        if kind not in kinds:
            kinds.append(kind)
    return kinds or list(DEFAULT_EDGE_KINDS)


def _peer_entry(
    *, direction: str, kind: str, doc: dict, peer_id: str, broken: bool = False,
) -> dict:
    is_ref = bool(doc.get("is_reference"))
    return {
        "direction": direction,
        "kind": kind,
        "document_id": peer_id,
        "title": doc.get("title", "") if doc else "",
        "is_reference": is_ref,
        "source_type": "ref" if is_ref else "doc",
        # Outgoing stored edges only: target soft-deleted or gone. Never true
        # for incoming (deleted sources are dropped wholesale, not marked).
        "broken": broken,
        "updated_at": str(doc["updated_at"]) if doc and doc.get("updated_at") else None,
    }


async def _stored_triples(
    db, document_id: str, direction: str, wanted_stored: set[str],
) -> list[tuple[str, str, str]]:
    """(direction, kind, peer) from doc_mentions, coalescing NONE → 'mention'.

    # WHY the NONE coalesce: rows written before the kind field carry NONE —
    # SurrealDB DEFAULT never retro-fills existing rows, so the read — not the
    # backfill — is what keeps legacy edges answerable.
    """
    triples: list[tuple[str, str, str]] = []
    if direction in ("out", "both"):
        rows = await db.query(
            "SELECT out, kind FROM doc_mentions WHERE in = type::record('documents', $did)",
            {"did": document_id},
        )
        for r in (rows or []):
            tid = extract_id(r.get("out"))
            kind = r.get("kind") or KIND_MENTION
            if tid and kind in wanted_stored:
                triples.append(("out", kind, tid))
    if direction in ("in", "both"):
        rows = await db.query(
            "SELECT in, kind FROM doc_mentions WHERE out = type::record('documents', $did)",
            {"did": document_id},
        )
        for r in (rows or []):
            sid = extract_id(r.get("in"))
            kind = r.get("kind") or KIND_MENTION
            if sid and kind in wanted_stored:
                triples.append(("in", kind, sid))
    return triples


async def _attachment_triples(db, row: dict, direction: str) -> list[tuple[str, str, str]]:
    """attachment projection: host → each live material it carries (out), and
    the host of THIS row when it is itself a material (in)."""
    triples: list[tuple[str, str, str]] = []
    document_id = extract_id(row.get("id")) or ""
    if direction in ("out", "both") and document_id:
        mats = await db.query(
            "SELECT meta::id(id) AS id FROM documents "
            "WHERE parent_id = $did AND is_reference = true AND deleted_at IS NONE",
            {"did": document_id},
        )
        for m in (mats or []):
            mid = str(m.get("id") or "")
            if mid:
                triples.append(("out", KIND_ATTACHMENT, mid))
    if direction in ("in", "both") and row.get("is_reference") and row.get("parent_id"):
        triples.append(("in", KIND_ATTACHMENT, str(row["parent_id"])))
    return triples


async def _chat_label_fallbacks(db, untitled_ids: list[str]) -> dict[str, str]:
    """First user-message preview per untitled chat — the label a chat with no
    `title` falls back to (plan: never the anchor document's title)."""
    rows = await db.query(
        f"SELECT chat_id, string::slice(content, 0, {PREVIEW_CONTENT_MAX}) AS content "
        "FROM messages WHERE chat_id IN $ids AND role = 'user' AND deleted_at IS NONE "
        "ORDER BY created_at ASC",
        {"ids": untitled_ids},
    )
    fallback: dict[str, str] = {}
    for m in (rows or []):
        cid = str(m.get("chat_id") or "")
        if cid and cid not in fallback:
            fallback[cid] = (m.get("content") or "").strip()
    return fallback


async def _scope_allowed(scope_root: str | None, peer_ids: list[str], project_id: str) -> set[str]:
    """Peer ids allowed under a subtree-scoped agent key (empty scope → all).

    # INVARIANT: scope is a CEILING on top of the project wall, never a replacement.
    # Why: it only narrows what a scoped key sees — it cannot widen access, and
    # replacing the wall with it would let a scoped key name cross-project peers
    # the RBAC wall already refuses. Document peers only; note/chat peers are
    # anchored to the in-scope document itself. Membership is computed once (via
    # scope.subtree_doc_ids — one project scan, and the INDEX rule: a key on the
    # project's index sees the whole project minus system/help) instead of one
    # ancestor walk per peer. The early `if not scope_root` return is
    # load-bearing: subtree_doc_ids returns [] for an empty scope, meaning "no
    # filter", not "nothing allowed".
    """
    if not scope_root or not peer_ids:
        return set(peer_ids)
    allowed = set(await subtree_doc_ids(scope_root, project_id))
    return {pid for pid in peer_ids if pid in allowed}


def _walled_document_entries(
    triples: list[tuple[str, str, str]],
    *,
    alive: dict[str, dict],
    allowed_peers: set[str],
    project_id: str,
) -> list[dict]:
    """Project-wall + dedup the document-peer triples into entries.

    # INVARIANT(security): a peer outside the reading document's project is skipped.
    # Why: doc_mentions edges survive cross-project subtree moves (documents/move.py
    # never rewrites them), so without the filter a member of the target project would
    # learn the title and updated_at of documents in a project he cannot read. Project
    # equality is the wall — per-source access resolution is deliberately absent
    # (small trusted group; N×4 round-trips otherwise, audit N4).
    """
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for direction, kind, peer_id in triples:
        key = (direction, kind, peer_id)
        if key in seen:
            continue
        seen.add(key)
        if peer_id not in allowed_peers:
            continue
        peer = alive.get(peer_id)
        if peer is not None and peer.get("project_id") != project_id:
            continue  # project wall — the peer is invisible, not broken
        if peer is None:
            if direction == "out" and kind in STORED_EDGE_KINDS:
                # Outgoing stored edge to a soft-deleted/gone target: kept, marked.
                out.append(_peer_entry(
                    direction=direction, kind=kind, doc={}, peer_id=peer_id, broken=True,
                ))
            # Incoming from a deleted/gone source (or a projected attachment whose
            # row vanished) is dropped — it is not the reader's business.
            continue
        out.append(_peer_entry(direction=direction, kind=kind, doc=peer, peer_id=peer_id))
    return out


async def _session_entries(db, note_rows: list, chat_rows: list) -> list[dict]:
    """note/chat peer entries — anchored to the reading document, so the only
    wall that applies is chat-context's own-chats filter (already in the query).

    # INVARIANT(security): chat-context returns ONLY the viewer's own chats.
    # Why: chats are per-user rows; another member's private chat must never be
    # revealed by its context — the projection query keys on the viewer's
    # user_id, so a foreign chat cannot enter chat_rows at all.
    """
    out: list[dict] = []
    if note_rows:
        previews = await note_previews(db, [str(r["sid"]) for r in note_rows if r.get("sid")])
        for r in note_rows:
            sid = str(r.get("sid") or "")
            if not sid:
                continue
            prev = previews.get(sid) or {}
            out.append({
                "direction": "in",
                "kind": KIND_NOTE,
                "session_id": sid,
                "label": prev.get("first") or "",
                "author": r.get("user_id"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            })
    if chat_rows:
        untitled = [
            str(r["sid"]) for r in chat_rows if not (r.get("title") or "").strip() and r.get("sid")
        ]
        fallbacks = await _chat_label_fallbacks(db, untitled) if untitled else {}
        for r in chat_rows:
            sid = str(r.get("sid") or "")
            if not sid:
                continue
            title = (r.get("title") or "").strip()
            out.append({
                "direction": "in",
                "kind": KIND_CHAT_CONTEXT,
                "session_id": sid,
                "label": title or fallbacks.get(sid, ""),
                "updated_at": str(r["updated_at"]) if r.get("updated_at") else None,
            })
    return out


def _validate_query(direction: str, kinds: list[str] | None) -> list[str]:
    """Reject unknown direction/kind with 422; return the effective kind list."""
    if direction not in ("in", "out", "both"):
        raise HTTPException(status_code=422, detail=f"Unknown direction: {direction!r}")
    wanted = list(kinds) if kinds else list(DEFAULT_EDGE_KINDS)
    for kind in wanted:
        if kind not in ALL_EDGE_KINDS:
            raise HTTPException(status_code=422, detail=f"Unknown edge kind: {kind!r}")
    return wanted


async def _note_rows(db, document_id: str) -> list:
    """Notes with chat_sessions.document_id = THIS document — the DIRECT anchor
    only (operator Ruling: the branch-scope walk is a listing convenience, not
    a relation)."""
    return await db.query(
        "SELECT meta::id(id) AS sid, user_id, created_at FROM chat_sessions "
        "WHERE document_id = $did AND is_note = true AND deleted_at IS NONE",
        {"did": document_id},
    ) or []


async def _chat_context_rows(db, project_id: str, viewer_user_id: str, document_id: str) -> list:
    """Chats using this document as context — the VIEWER's own only (the
    user_id key in the query is the whole wall; see _session_entries)."""
    return await db.query(
        "SELECT meta::id(id) AS sid, title, updated_at FROM chat_sessions "
        "WHERE project_id = $pid AND user_id = $uid AND is_note = false "
        "AND deleted_at IS NONE AND context_document_ids CONTAINS $did",
        {"pid": project_id, "uid": viewer_user_id, "did": document_id},
    ) or []


def _external_entries(document_id: str, row: dict) -> list[dict]:
    """external projection: outgoing `[t](https://…)` links parsed from the GFM
    read-model — the SAME live-session-else-DB-row branch get_document_links
    uses (routes/documents.py). A URL is not a graph node: no wall, no
    project_id, entries carry {direction, kind, url, label} — the shape the
    Links panel's external section renders (plan table-links-notes). Out-only:
    the caller gates on direction like the note/chat projections do on `in`."""
    from collab.registry import (
        get_active_session,  # lazy: registry is heavy and cycle-prone
    )

    session = get_active_session("doc", document_id)
    if session and session.clients:
        from table_serialize import expand_tables
        content = expand_tables(session.ydoc)
    else:
        content = row.get("content") or ""
    return [
        {"direction": "out", "kind": KIND_EXTERNAL, "url": url, "label": label}
        for label, url in extract_external_links(content)
    ]


async def collect_edges(
    document_id: str,
    *,
    viewer_user_id: str,
    direction: str = "both",
    kinds: list[str] | None = None,
    scope_root: str | None = None,
) -> dict:
    """The typed edges of one document OR reference, both directions, walled.

    Entry shape — document peers: {direction, kind, document_id, title,
    is_reference, source_type, broken (outgoing only), updated_at};
    note/chat peers: {direction: 'in', kind, session_id, label} plus note
    (author, created_at) / chat (updated_at); external (out-only, opt-in):
    {direction: 'out', kind, url, label} — no document_id.
    """
    wanted = _validate_query(direction, kinds)

    db = await get_db()
    row = await fetch_one("documents", document_id)
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")
    project_id = row.get("project_id") or ""

    triples = await _stored_triples(
        db, document_id, direction, {k for k in wanted if k in STORED_EDGE_KINDS},
    )
    if KIND_ATTACHMENT in wanted:
        triples += await _attachment_triples(db, row, direction)

    note_rows: list = []
    if KIND_NOTE in wanted and direction in ("in", "both"):
        note_rows = await _note_rows(db, document_id)
    chat_rows: list = []
    if KIND_CHAT_CONTEXT in wanted and direction in ("in", "both"):
        chat_rows = await _chat_context_rows(db, project_id, viewer_user_id, document_id)
    external_entries: list[dict] = []
    if KIND_EXTERNAL in wanted and direction in ("out", "both"):
        external_entries = _external_entries(document_id, row)

    peer_ids = {peer_id for _d, _k, peer_id in triples}
    allowed = await _scope_allowed(scope_root, sorted(peer_ids), project_id)
    alive = await fetch_many("documents", sorted(peer_ids)) if peer_ids else {}
    entries = _walled_document_entries(
        triples, alive=alive, allowed_peers=allowed, project_id=project_id,
    )
    entries += await _session_entries(db, note_rows, chat_rows)
    entries += external_entries
    return {"edges": entries}
