"""B5: pinned-region session flag + region wire model.

# SYSTEM: chat-region-session-tests — has_region flag round-trip + RegionRef validation.

A pinned-region agent chat forces confirm mode and is constrained to a text span.
The backend persists ONLY `chat_sessions.has_region` (server-authoritative confirm
forcing); the RelativePosition pair is frontend-owned. This file covers the
backend-owned pieces: the model invariants, persistence on create, serialization,
PATCH (unpin), and the `region` field on CompletionRequest (prompt-hint carrier).
"""

import pytest
from pydantic import ValidationError

from models import CompletionRequest, RegionRef, SessionCreate

# ─── RegionRef ────────────────────────────────────────────────────────────────


def test_region_ref_basic():
    r = RegionRef(doc_id="doc-1", from_cp=0, to_cp=5)
    assert r.doc_id == "doc-1"
    assert r.from_cp == 0
    assert r.to_cp == 5
    assert r.text is None  # text is optional (frontend-owned, live-resolved)


def test_region_ref_rejects_inverted_offsets():
    with pytest.raises(ValidationError, match="from_cp"):
        RegionRef(doc_id="doc-1", from_cp=10, to_cp=5)


def test_region_ref_allows_empty_region():
    # A collapsed region (from==to) is valid on the wire — the backend rejects it
    # as out-of-scope at apply, but the MODEL must accept it (the frontend may
    # legitimately resolve a collapsed anchor after user deletions).
    r = RegionRef(doc_id="doc-1", from_cp=5, to_cp=5)
    assert r.from_cp == r.to_cp == 5


# ─── SessionCreate.has_region ─────────────────────────────────────────────────


def test_session_create_has_region_default_false():
    s = SessionCreate(project_id="p-1", document_id="d-1")
    assert s.has_region is False


def test_session_create_accepts_has_region_for_non_note():
    # plan: remove-ask-line-mode-axis (D4(2)): the `has_region requires agent
    # mode` guard is deleted — every AI chat is an agent chat, so a non-note
    # session with has_region=true is accepted.
    s = SessionCreate(
        project_id="p-1", document_id="d-1", has_region=True,
    )
    assert s.has_region is True


def test_session_create_rejects_has_region_for_note_session():
    # plan: remove-ask-line-mode-axis (D4(3)): the note-incompatibility rule is
    # re-expressed on the is_note axis — a pinned region only makes sense for an
    # agent that edits documents.
    with pytest.raises(ValidationError, match="has_region is incompatible with note sessions"):
        SessionCreate(project_id="p-1", document_id="d-1", is_note=True, has_region=True)


def test_session_create_has_region_defaults_target_doc():
    # The agent-mode target_doc_id derivation now runs for every non-note session
    # (plan: remove-ask-line-mode-axis, D4(1)).
    s = SessionCreate(
        project_id="p-1", document_id="d-1", has_region=True,
    )
    assert s.target_doc_id == "d-1"


# ─── CompletionRequest.region ─────────────────────────────────────────────────


def test_completion_request_region_optional():
    body = CompletionRequest(messages=[{"role": "user", "content": "hi"}])
    assert body.region is None


def test_completion_request_carries_region():
    body = CompletionRequest(
        messages=[{"role": "user", "content": "edit this"}],
        region=RegionRef(doc_id="d-1", from_cp=0, to_cp=4, text="edit"),
    )
    assert body.region is not None
    assert body.region.doc_id == "d-1"
    assert body.region.text == "edit"


# ─── serializer ───────────────────────────────────────────────────────────────


def test_serialize_session_exposes_has_region_default_false():
    from chat_sessions.serialize import serialize_session

    out = serialize_session({"id": "chat_sessions:s1", "has_region": None})
    assert out["has_region"] is False


def test_serialize_session_exposes_has_region_true():
    from chat_sessions.serialize import serialize_session

    out = serialize_session({"id": "chat_sessions:s1", "has_region": True})
    assert out["has_region"] is True
