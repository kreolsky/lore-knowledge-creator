"""Unit tests for SessionCreate validator — note anchor semantics.

# SYSTEM: session-create-validation-tests — guards anchored/anchorless notes contract

Anchorless notes are first-class (the "Add Note" button); an anchored note carries
both relative positions or neither.
"""

import pytest
from pydantic import ValidationError

from models import SessionCreate

# A v1 Yjs RelativePosition (item scope, client 7, clock 3, assoc 0), base64.
RELPOS = "AAcDAA=="


def test_note_without_anchors_is_valid():
    # INVARIANT under test: anchorless notes are first-class — payload from
    # createAnchorlessSession() must validate.
    session = SessionCreate(
        project_id="proj_1",
        document_id="doc_1",
        is_note=True,
    )
    assert session.is_note is True
    assert session.anchor_rel_start is None
    assert session.anchor_rel_end is None


def test_note_with_both_anchors_is_valid():
    session = SessionCreate(
        project_id="proj_1",
        document_id="doc_1",
        is_note=True,
        anchor_rel_start=RELPOS,
        anchor_rel_end=RELPOS,
    )
    assert session.anchor_rel_start == RELPOS
    assert session.anchor_rel_end == RELPOS


def test_note_with_only_start_anchor_is_rejected():
    with pytest.raises(ValidationError, match="must be set together"):
        SessionCreate(
            project_id="proj_1",
            document_id="doc_1",
            is_note=True,
            anchor_rel_start=RELPOS,
        )


def test_note_with_only_end_anchor_is_rejected():
    with pytest.raises(ValidationError, match="must be set together"):
        SessionCreate(
            project_id="proj_1",
            document_id="doc_1",
            is_note=True,
            anchor_rel_end=RELPOS,
        )

