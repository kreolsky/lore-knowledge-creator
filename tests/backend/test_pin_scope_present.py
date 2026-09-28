"""Positive guard: the pinned-region (SelectionRef) feature IS present.

# SYSTEM: chat-region-feature-presence-tests — locks the reintroduced feature in.

Replaces the former `test_pin_scope_removed.py` guard (which forbade these tokens
after the feature was cut in 7d3d0d9 / 5710e59). The feature is now redesigned and
reintroduced (plan: selection-region-agent), so this file asserts the KEY symbols
exist instead of forbidding them. Keeps a regression-preventing sentinel: if the
feature is cut again, this test fails loudly rather than silently disappearing.
"""

from __future__ import annotations


def test_region_ref_model_exists():
    from models import RegionRef

    r = RegionRef(doc_id="d", from_cp=0, to_cp=1)
    assert r.doc_id == "d"


def test_session_create_has_region_field_exists():
    import inspect

    from models import SessionCreate

    assert "has_region" in inspect.signature(SessionCreate).parameters


def test_completion_request_region_field_exists():
    import inspect

    from models import CompletionRequest

    assert "region" in inspect.signature(CompletionRequest).parameters




def test_apply_path_carries_the_region():
    """The region reaches the apply primitive, which is what enforces containment.

    It no longer rides an apply-MODE cell: a pinned edit applies directly and the
    boundary is checked at the write, so `apply_edits_to_document` taking the region
    is the sentinel. Escaping edits are rejected as `region_locked` — asserted in
    test_proposal_deletion_survival.
    """
    import inspect

    from agent.tool_api_surface import apply_edits_to_document

    assert "region" in inspect.signature(apply_edits_to_document).parameters


def test_surgical_convergence_tail_exists():
    from agent.doc_state import route_document_edits

    assert route_document_edits.__name__ == "route_document_edits"
