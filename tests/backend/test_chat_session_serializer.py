"""Regression tests for chat session serialization.

Pins Bug 1 fix: anchor_rel_start / anchor_rel_end (raw Yjs RelativePosition
bytes, NOT valid UTF-8) must never reach the API surface. FastAPI's
jsonable_encoder runs bytes.decode() on them and crashes the whole list
response with a 500 (UnicodeDecodeError) on docs whose notes carry a Yjs
anchor.
"""
from chat_sessions.serialize import serialize_session
from fastapi.encoders import jsonable_encoder


def _note_row(is_note: bool = True, session_id: str = "chat_sessions:abc") -> dict:
    """A chat_sessions row as returned by SELECT *."""
    return {
        "id": session_id,
        "title": "Note session",
        "document_id": "documents:doc1",
        "user_id": "users:u1",
        "project_id": "projects:p1",
        "mode": "chat",
        "is_note": is_note,
        "context_document_ids": [],
        "anchor_offset_start": 10,
        "anchor_offset_end": 20,
        "anchor_rel_start": b"\x80\x01\x02",  # NOT valid UTF-8 (0x80)
        "anchor_rel_end": b"\x80\x03\x04",
        "created_at": None,
    }


def test_serialize_session_strips_anchor_rel_bytes_keys():
    """anchor_rel_start/end must be absent from the serialized output."""
    out = serialize_session(_note_row())
    assert "anchor_rel_start" not in out
    assert "anchor_rel_end" not in out


def test_serialize_session_output_is_json_serializable():
    """The whole output must survive FastAPI jsonable_encoder (which calls
    bytes.decode() on any bytes value) without raising UnicodeDecodeError."""
    out = serialize_session(_note_row())
    # If anchor_rel_* leaked, jsonable_encoder raises UnicodeDecodeError here.
    encoded = jsonable_encoder(out)
    assert isinstance(encoded, dict)
    # Belt-and-suspenders: confirm no bytes survived anywhere in the output.
    import json
    json.dumps(encoded)


# ─── Note display derivation (plan: notes-system-refactor-image-attachments) ─
# INVARIANT: note pill text is DERIVED from the messages preview map, never from
# `title`. The serializer consumes a pre-built previews map (Decision 2) and
# itself performs ZERO DB calls — the grouped query lives in list_sessions.


def test_serialize_note_session_derives_previews_from_map():
    """A note session with a populated preview map exposes derived fields.

    The map is keyed by the BARE session id (extract_id of the row's RecordID),
    matching how list_sessions builds it and how the serializer looks it up
    (out["session_id"]). Keying by the full "chat_sessions:..." form would never
    match — this is the regression guard for the Surreal id-form join.

    last_message_at is sourced from the UNIFIED last_at_by_session map (plan:
    chat-sort-by-last-message), which list_sessions seeds from the preview's
    last_at for notes + the AI timestamp aggregate. Passing it here mirrors that
    flow; omitting it leaves last_message_at=None (see the next test).
    """
    row = _note_row(session_id="chat_sessions:n1")
    previews = {
        "n1": {
            "first": "First message text",
            "last": "Last message text",
            "count": 3,
            "last_at": "2026-07-01T10:00:00Z",
        },
    }
    out = serialize_session(
        row,
        previews_by_session_id=previews,
        last_at_by_session={"n1": "2026-07-01T10:00:00Z"},
    )
    assert out["first_message_preview"] == "First message text"
    assert out["last_message_preview"] == "Last message text"
    assert out["message_count"] == 3
    assert out["last_message_at"] == "2026-07-01T10:00:00Z"


def test_serialize_note_session_single_message_equal_first_last():
    """A single-message note has equal first/last previews."""
    row = _note_row(session_id="chat_sessions:n1")
    previews = {
        "n1": {
            "first": "only message", "last": "only message",
            "count": 1, "last_at": "2026-07-01T10:00:00Z",
        },
    }
    out = serialize_session(row, previews_by_session_id=previews)
    assert out["first_message_preview"] == out["last_message_preview"] == "only message"
    assert out["message_count"] == 1


def test_serialize_note_session_without_previews_is_safe():
    """A note session with no preview map entry yields nullish derived fields
    (e.g. a brand-new empty note) — never raises, never reads `title`."""
    out = serialize_session(_note_row(session_id="chat_sessions:empty"))
    assert out["first_message_preview"] is None
    assert out["last_message_preview"] is None
    assert out["message_count"] == 0
    assert out["last_message_at"] is None


def test_serialize_ai_session_has_no_preview_fields():
    """AI chats (is_note=false) never receive the note display preview fields
    (first/last/count). last_message_at is serialized for EVERY session now
    (plan: chat-sort-by-last-message) — its presence on AI chats is pinned in
    test_list_sessions_sort.py, not here."""
    row = _note_row(is_note=False, session_id="chat_sessions:ai1")
    previews = {"ai1": {"first": "x", "last": "y", "count": 5, "last_at": "z"}}
    out = serialize_session(row, previews_by_session_id=previews)
    assert "first_message_preview" not in out
    assert "last_message_preview" not in out
    assert "message_count" not in out


# ─── context_reference_ids (plan: chat-context-id-invariant-persistence, A.1) ─
# INVARIANT: the server is the single source of truth for the doc/ref split.
# `context_reference_ids` is the ordered subset of context_ids whose id is a
# reference (is_reference=true in ref_map). The frontend hydrateFromSessions
# consumes it directly so a cross-doc reference (outside the open document's
# scope) is never misclassified as a document — that misclassification once
# caused a "partial reset" (References-tab checkbox unchecked) and, combined
# with the prune effect, an irreversible PATCH that dropped the selection.


def _ai_row(session_id="chat_sessions:ai", context_ids=None):
    """An AI (non-note) chat_sessions row with the given context ids."""
    row = _note_row(is_note=False, session_id=session_id)
    row["context_document_ids"] = list(context_ids or [])
    return row


def test_serialize_session_context_reference_ids_lists_ref_ids_in_order():
    """A mixed doc+ref context yields context_reference_ids = the ref ids in
    their original insertion order (not ref_map key order, not sorted)."""
    ref_map = {
        "documents:refB": {"is_reference": True, "parent_id": "documents:docA", "title": "Ref B"},
        "documents:refC": {"is_reference": True, "parent_id": "documents:docA", "title": "Ref C"},
    }
    out = serialize_session(
        _ai_row(context_ids=["documents:docA", "documents:refB", "documents:refC", "documents:docD"]),
        ref_map,
    )
    assert out["context_ids"] == ["documents:docA", "documents:refB", "documents:refC", "documents:docD"]
    assert out["context_reference_ids"] == ["documents:refB", "documents:refC"]


def test_serialize_session_context_reference_ids_ignores_title_rename():
    """The split is id-based: changing the reference title in ref_map does not
    change which ids classify as references."""
    ref_map_before = {"documents:refB": {"is_reference": True, "parent_id": "documents:docA", "title": "Old Title"}}
    ref_map_after = {"documents:refB": {"is_reference": True, "parent_id": "documents:docA", "title": "New Title"}}
    ids_before = serialize_session(_ai_row(context_ids=["documents:docA", "documents:refB"]), ref_map_before)["context_reference_ids"]
    ids_after = serialize_session(_ai_row(context_ids=["documents:docA", "documents:refB"]), ref_map_after)["context_reference_ids"]
    assert ids_before == ids_after == ["documents:refB"]


def test_serialize_session_note_context_reference_ids_empty():
    """A note session forces both context_ids and context_reference_ids empty
    (notes never carry content context)."""
    ref_map = {"documents:refB": {"is_reference": True, "parent_id": None, "title": "Ref B"}}
    out = serialize_session(_note_row(), ref_map)
    assert out["context_ids"] == []
    assert out["context_reference_ids"] == []


def test_serialize_session_context_reference_ids_empty_when_all_docs():
    """A context of only documents yields an empty reference id list."""
    out = serialize_session(_ai_row(context_ids=["documents:docA", "documents:docD"]), {})
    assert out["context_reference_ids"] == []
