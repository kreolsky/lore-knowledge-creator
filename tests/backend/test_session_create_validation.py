"""Unit tests for SessionCreate validator — note anchor semantics.

# SYSTEM: session-create-validation-tests — guards anchored/anchorless notes contract

Regression coverage for commit 134c8cd, which made anchor_offset_*
unconditionally required on note sessions and broke the "Add Note"
button (anchorless notes).
"""

import pytest
from pydantic import ValidationError

from models import SessionCreate


def test_note_without_anchors_is_valid():
    # INVARIANT under test: anchorless notes are first-class — payload from
    # createAnchorlessSession() must validate.
    session = SessionCreate(
        project_id="proj_1",
        document_id="doc_1",
        is_note=True,
    )
    assert session.is_note is True
    assert session.anchor_offset_start is None
    assert session.anchor_offset_end is None


def test_note_with_both_anchors_is_valid():
    session = SessionCreate(
        project_id="proj_1",
        document_id="doc_1",
        is_note=True,
        anchor_offset_start=10,
        anchor_offset_end=20,
    )
    assert session.anchor_offset_start == 10
    assert session.anchor_offset_end == 20


def test_note_with_only_start_anchor_is_rejected():
    with pytest.raises(ValidationError, match="must be set together"):
        SessionCreate(
            project_id="proj_1",
            document_id="doc_1",
            is_note=True,
            anchor_offset_start=10,
        )


def test_note_with_only_end_anchor_is_rejected():
    with pytest.raises(ValidationError, match="must be set together"):
        SessionCreate(
            project_id="proj_1",
            document_id="doc_1",
            is_note=True,
            anchor_offset_end=20,
        )


def test_note_with_inverted_anchors_is_rejected():
    with pytest.raises(ValidationError, match=">= anchor_offset_start"):
        SessionCreate(
            project_id="proj_1",
            document_id="doc_1",
            is_note=True,
            anchor_offset_start=20,
            anchor_offset_end=10,
        )
