"""Document inbox — the unread flag on externally arrived objects.

# SYSTEM: inbox — one pool per document: a note or reference that arrived from
OUTSIDE (a widget key, or an MCP key over the gateway) carries an unread flag
for the key OWNER. A document with any flagged object paints its tree row (and
its ancestors) sticky-yellow; opening the object clears its flag. There are no
notification records — the flag is a field on the object itself.

# ARCH: this module owns the WHOLE mechanism. `mark_arrived` is the ONLY writer
of `unread_for`; every external surface (widget routes, the MCP gateway's
reference-creating dispatch, the attach_file redeem route) calls it and nothing
else writes the field. Why one home: the flag has one meaning ("arrived from
outside, for you, unopened") and three ingress surfaces — a second writer would
drift on the toggle semantics, the event payload, or the recipient rule.

External = a key that is not `internal` (api_key_auth) — the dsh chat agent's
internal key never flags. Recipient = the key owner (ctx["user_id"]). Other
members see the same objects, never the flag: serializers expose `unread: bool`
for the VIEWER, never the raw user id.

Per (user x document) toggles live in the existing user_preferences.preferences
FLEXIBLE object as `doc_notify.<document_id> = {notes: bool, refs: bool}`;
defaults notes ON, refs OFF. `mark_arrived` reads the toggle for its kind: OFF
means the object is created unflagged — missed = lost (the flag is a field on
the object, not a queue; nothing replays).

Deleted objects never count: every reader filters `deleted_at IS NONE` on the
object AND on its host document. Deleting does not clear the flag — one writer
per field; the filter makes a deleted flagged object vanish from the pool for
everyone alike.
"""
from __future__ import annotations

import logging

from db import fetch_one, get_db
from event_bus import emit

logger = logging.getLogger(__name__)

KIND_NOTE = "note"
KIND_REF = "ref"
# kind → the table the flag lives on. A note is a chat_sessions row with
# is_note=true; a reference is a documents row with is_reference=true. The
# one writer (mark_arrived) keys the UPDATE off this map.
_TABLES = {KIND_NOTE: "chat_sessions", KIND_REF: "documents"}


def _table(kind: str) -> str:
    if kind not in _TABLES:
        raise ValueError(f"inbox: unknown kind {kind!r} (expected 'note' | 'ref')")
    return _TABLES[kind]


async def doc_notify(project_id: str, user_id: str, document_id: str) -> dict:
    """The (user x document) notify toggles: {notes: bool, refs: bool}.

    Defaults: notes ON (a note that arrived for you is attention by default),
    refs OFF (external file deposits are routine and would flag every upload).
    """
    db = await get_db()
    rows = await db.query(
        "SELECT preferences FROM user_preferences "
        "WHERE user_id = $uid AND project_id = $pid LIMIT 1",
        {"uid": user_id, "pid": project_id},
    )
    # No row yet = the user never saved preferences in this project — the
    # defaults answer (notes ON, refs OFF), not an error.
    entry = ((rows[0] if rows else {}) or {}).get("preferences") or {}
    toggles = entry.get("doc_notify") or {}
    toggles = toggles.get(document_id) or {}
    return {
        "notes": bool(toggles.get("notes", True)),
        "refs": bool(toggles.get("refs", False)),
    }


async def set_doc_notify(
    project_id: str, user_id: str, document_id: str,
    *, notes: bool | None = None, refs: bool | None = None,
) -> dict:
    """Upsert the toggles for ONE document via a nested-path write.

    # INVARIANT(corruption): only the `preferences.doc_notify` subtree is
    # written — never the whole `preferences` object. Why: the blob is FLEXIBLE
    # pass-through state co-owned by the frontend ui-store; replacing it
    # wholesale from here would race a concurrent ui-store save and silently
    # drop its keys. The nested SET (the documents/move.py
    # last_accessed_doc_id precedent) touches one key.
    """
    current = await doc_notify(project_id, user_id, document_id)
    if notes is not None:
        current["notes"] = notes
    if refs is not None:
        current["refs"] = refs
    db = await get_db()
    rows = await db.query(
        "SELECT preferences FROM user_preferences "
        "WHERE user_id = $uid AND project_id = $pid LIMIT 1",
        {"uid": user_id, "pid": project_id},
    )
    # No row yet → the doc_notify map starts empty (the UPSERT below creates the
    # row). Merge into the WHOLE current map so a toggle on document A never
    # drops document B's toggles, then write the subtree back in ONE statement.
    doc_notify_map = dict((((rows[0] if rows else {}) or {}).get("preferences") or {}).get("doc_notify") or {})
    doc_notify_map[document_id] = current
    await db.query(
        "UPSERT user_preferences SET user_id = $uid, project_id = $pid, "
        "updated_at = time::now(), preferences.doc_notify = $map "
        "WHERE user_id = $uid AND project_id = $pid",
        {"uid": user_id, "pid": project_id, "map": doc_notify_map},
    )
    return current


async def mark_arrived(
    kind: str, *, object_id: str, project_id: str, document_id: str,
    recipient_id: str,
) -> None:
    """Flag ONE externally arrived object for its recipient — the ONLY writer
    of `unread_for` in the codebase.

    The toggle for the kind is consulted FIRST: OFF means the object is created
    unflagged and nothing else happens (missed = lost). On flag, emits
    `inbox_changed {project_id, user_id, document_id}` on the event bus — the
    project-ws handler delivers it owner-only (never a _SUBSCRIPTIONS entry:
    that broadcasts to every member, and the flag is per-user).
    """
    toggles = await doc_notify(project_id, recipient_id, document_id)
    if not toggles.get("notes" if kind == KIND_NOTE else "refs"):
        return
    db = await get_db()
    await db.query(
        "UPDATE type::record($table, $id) SET unread_for = $uid",
        {"table": _table(kind), "id": object_id, "uid": recipient_id},
    )
    await emit("inbox_changed", project_id=project_id, user_id=recipient_id,
               document_id=document_id)


async def mark_read(kind: str, object_id: str, user_id: str) -> bool:
    """Read = OPENED: clear the flag iff it equals the caller, else no-op.

    Returns True when this call cleared a flag (and emitted); False when there
    was nothing for this user to clear. The equality IS the authorization — a
    flag never equals a non-recipient, so no access check is needed here.
    """
    table = _table(kind)
    row = await fetch_one(table, object_id)
    if not row or row.get("unread_for") != user_id:
        return False
    project_id = row.get("project_id") or ""
    document_id = row.get("document_id") or row.get("parent_id") or ""
    db = await get_db()
    await db.query(
        "UPDATE type::record($table, $id) SET unread_for = NONE",
        {"table": table, "id": object_id},
    )
    await emit("inbox_changed", project_id=project_id, user_id=user_id,
               document_id=document_id)
    return True


async def summary(project_id: str, user_id: str) -> dict:
    """The caller's unread pool: {documents: {<document_id>: {notes: n, refs: n}}}.

    Counts flagged, LIVE objects only: `deleted_at IS NONE` on the object and on
    its host document (notes' document_id / references' parent_id). Ancestors
    are derived from the tree on the client, not here.
    """
    db = await get_db()
    # Serial reads on the shared conn (the widget_info precedent — never gather).
    # The alive-host set is resolved FIRST so the grouped counts stay simple
    # index-served queries with no per-row subquery. Hosts are tree documents
    # only — references never host a flagged object, and keeping them out
    # keeps the IN list the size of the tree, not of every upload.
    alive = await db.query(
        "SELECT VALUE meta::id(id) FROM documents "
        "WHERE project_id = $pid AND deleted_at IS NONE AND is_reference != true",
        {"pid": project_id},
    )
    alive = [str(h) for h in (alive or [])]
    if not alive:
        return {"documents": {}}
    note_rows = await db.query(
        "SELECT document_id AS host, count() AS n FROM chat_sessions "
        "WHERE project_id = $pid AND unread_for = $uid AND is_note = true "
        "AND deleted_at IS NONE AND document_id IN $alive "
        "GROUP BY document_id",
        {"pid": project_id, "uid": user_id, "alive": alive},
    )
    ref_rows = await db.query(
        "SELECT parent_id AS host, count() AS n FROM documents "
        "WHERE project_id = $pid AND unread_for = $uid AND is_reference = true "
        "AND deleted_at IS NONE AND parent_id IN $alive "
        "GROUP BY parent_id",
        {"pid": project_id, "uid": user_id, "alive": alive},
    )
    out: dict[str, dict[str, int]] = {}
    for r in (note_rows or []):
        host = str(r.get("host") or "")
        if host:
            out.setdefault(host, {"notes": 0, "refs": 0})["notes"] = int(r.get("n") or 0)
    for r in (ref_rows or []):
        host = str(r.get("host") or "")
        if host:
            out.setdefault(host, {"notes": 0, "refs": 0})["refs"] = int(r.get("n") or 0)
    return {"documents": out}


__all__ = [
    "KIND_NOTE", "KIND_REF",
    "doc_notify", "set_doc_notify", "mark_arrived", "mark_read", "summary",
]
