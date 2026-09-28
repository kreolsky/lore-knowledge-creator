"""Chat-session wire shape: the session serializer and the reference map it reads."""

from db import serialize_record
from models import is_ref_row


async def build_ref_map(db, *doc_ids: str | None) -> dict[str, dict]:
    """Fetch is_reference + parent_id for a set of document IDs.

    Used by serialize_session to restore the legacy reference_id and
    split context_document_ids into document / reference lists.
    """
    ids = {d for d in doc_ids if d}
    if not ids:
        return {}
    rows = await db.query(
        "SELECT meta::id(id) AS id, is_reference, parent_id, title FROM documents "
        "WHERE meta::id(id) IN $ids AND is_reference = true AND deleted_at IS NONE",
        {"ids": list(ids)},
    )
    out: dict[str, dict] = {}
    for rr in (rows or []):
        out[str(rr["id"])] = {
            "is_reference": rr.get("is_reference", False),
            "parent_id": rr.get("parent_id"),
            "title": rr.get("title"),
        }
    return out


async def build_session_ref_map(db, row: dict) -> dict[str, dict]:
    """The ref map over a session row's own document_id plus every context id."""
    # WHY: Include context IDs in ref_map so serialize_session can correctly
    # split context_document_ids into doc IDs vs ref IDs AND emit the additive
    # context_reference_ids field (id-invariant classification for the frontend).
    all_ids = {row.get("document_id")}
    for cid in (row.get("context_document_ids") or []):
        all_ids.add(cid)
    return await build_ref_map(db, *all_ids)


def serialize_session(
    row: dict,
    ref_map: dict | None = None,
    previews_by_session_id: dict | None = None,
    last_at_by_session: dict | None = None,
    document_titles: dict[str, str] | None = None,
) -> dict:
    """Serialize a chat session row for the API surface.

    References are documents with is_reference=true.
    chat_sessions.document_id stores either a regular doc ID or a ref-doc ID.
    context_document_ids (DB column) stores ALL context IDs (docs + refs).

    API surface:
      - Unified context_ids array (insertion order preserved)
      - reference_id: computed READ-only when document_id points to a ref-doc
      - document_id: ref-doc id is mapped to its parent (owning document)

    Note display:
      - For note sessions, derived display fields (first/last message preview,
        message_count, last_message_at) are read from the `previews_by_session_id`
        map. The map is built by a single grouped query in list_sessions.
    """
    out = serialize_record(row, "session_id")
    _stamp_wire_fields(out)
    _stamp_reference_identity(out, ref_map or {}, document_titles)
    # ARCH: Note sessions never carry content context — reference_id preserved
    # for NotesPanel grouping and navigation.
    if out["is_note"]:
        _stamp_note_display(out, previews_by_session_id)
    # ARCH: last_message_at is derived for
    # EVERY session (notes + AI) from the unified last-activity map built in
    # list_sessions; the preview map's last_at is folded into the unified map, so
    # notes read the same value from it — one source. create/update_session
    # callers omit the map (None) → the field is None there, and the frontend
    # falls back to updated_at (correct for a freshly-created / just-PATCHed row).
    out["last_message_at"] = (last_at_by_session or {}).get(out.get("session_id", ""))
    return out


def _stamp_wire_fields(out: dict) -> None:
    """Normalize the row's own columns into their stable wire shape."""
    out["context_ids"] = list(out.get("context_document_ids") or [])
    out["is_note"] = bool(out.get("is_note"))
    # ARCH: the `mode` column stays in the schema (unread — no code reads
    # it) and is never emitted on the wire.
    out.pop("mode", None)
    # ARCH (agent_auto persistence): persisted "Full auto" dropdown state.
    # The client DERIVES the UI mode from agent_auto (deriveUIMode); the value is
    # authoritative on the session row (survives reload). Default false for
    # pre-field rows.
    out["agent_auto"] = bool(out.get("agent_auto"))
    # ARCH: the pinned-region flag is server-
    # authoritative (forces confirm + the containment gate). The client reads it to
    # render the SelectionPill + disable the auto-apply toggle. Default false for
    # pre-field rows.
    out["has_region"] = bool(out.get("has_region"))
    # ARCH: last-known context occupation for the token-usage gauge — the tokens
    # the active session occupies at the turn it was last written. Emitted verbatim
    # (null when absent / pre-migration). The client resolves the LIVE cap from
    # /models (context_windows); `used` is the only persisted context figure.
    out["context_tokens_used"] = out.get("context_tokens_used")
    # ARCH: stable wire shape — null when the
    # row predates the column or the value was cleared (Default), exactly like
    # context_tokens_used. The composer's dropdown reads null as Default.
    out["reasoning_effort"] = out.get("reasoning_effort")
    out.pop("context_document_ids", None)
    # INVARIANT(security): anchor_rel_* never serialized to client (raw Yjs bytes, not
    # UTF-8). Why: FastAPI's jsonable_encoder runs bytes.decode() on any bytes
    # value; Yjs RelativePosition bytes (e.g. 0x80) raise UnicodeDecodeError and
    # crash the WHOLE session list response with a 500. These fields are
    # presence-only hints today (see the writer-side INVARIANT in
    # chat_sessions/create.py) — dropped here at the single outbound
    # boundary, never sent to the client.
    out.pop("anchor_rel_start", None)
    out.pop("anchor_rel_end", None)


def _stamp_reference_identity(
    out: dict, ref_map: dict, document_titles: dict[str, str] | None,
) -> None:
    """Split context ids into references and label the session's parent entity."""
    # WHY: emit the ordered
    # subset of context_ids that are references (id in ref_map with
    # is_reference=true). The frontend hydrateFromSessions consumes this DIRECTLY
    # so a cross-doc reference is never misclassified as a document — the open
    # document's reference scope is never evidence of a reference's existence.
    # Why server-side: the route already builds ref_map from EVERY context id
    # (chat_sessions / sessions_list.py), so this adds zero DB queries; the client's
    # refIdSet was scope-limited to the open doc and silently flipped the split on
    # every doc/chat switch (a "partial reset" of the References tab).
    out["context_reference_ids"] = [
        cid for cid in out["context_ids"] if is_ref_row(ref_map.get(cid) or {})
    ]
    did_raw = out.get("document_id")
    ref_entry = ref_map.get(did_raw) if did_raw else None
    if ref_entry and is_ref_row(ref_entry):
        # WHY: a reference-scoped chat's parent IS the reference (ref: prefix),
        # never its owning document — even in split view (document_id NOT remapped).
        # Why: the chat's identity is the entity it was created on (user rule
        # 2026-07-09, recurring). The frontend labels a ref-row `ref: <title>` and
        # navigates to the reference, not the owning document.
        out["reference_id"] = did_raw
        out["reference_title"] = ref_entry.get("title")
        out["document_title"] = None
        return
    out["reference_id"] = None
    out["reference_title"] = None
    # ARCH: document_title labels a genuine
    # document-session with its own title (thin client). The map is
    # built by ONE batched query in list_sessions (zero DB calls here — the
    # serializer stays pure); create/update callers pass None → the field is None
    # and the client falls back to its document tree.
    out["document_title"] = (document_titles or {}).get(out.get("document_id"))


def _stamp_note_display(out: dict, previews_by_session_id: dict | None) -> None:
    """Clear a note's content context and stamp its message-preview fields."""
    out["context_ids"] = []
    out["context_reference_ids"] = []
    # WHY: note pill text is DERIVED from the messages preview map,
    # never from `title`. Why: `title` is a one-shot 100-char copy of the
    # first message (legacy "copied lines") that the backend never refreshes
    # — reading it for display shows stale text after edits/deletes. The
    # live preview map (built by one grouped query in list_sessions) is the
    # single current source. `serialize_session` itself performs ZERO DB
    # calls — the query lives in the route. AI chats do NOT receive these
    # fields.
    prev = (previews_by_session_id or {}).get(out.get("session_id", "")) or {}
    out["first_message_preview"] = prev.get("first")
    out["last_message_preview"] = prev.get("last")
    out["message_count"] = int(prev.get("count") or 0)
