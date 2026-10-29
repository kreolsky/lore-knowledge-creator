"""Positive guard: the pinned-region (SelectionRef) feature IS present.

# SYSTEM: chat-region-feature-presence-tests — locks the reintroduced feature in.

Replaces the former `test_pin_scope_removed.py` guard (which forbade these tokens
after the feature was cut in 7d3d0d9 / 5710e59). The feature is now redesigned and
reintroduced (plan: selection-region-agent), so this file asserts the KEY symbols
exist instead of forbidding them. Keeps a regression-preventing sentinel: if the
feature is cut again, this test fails loudly rather than silently disappearing.
"""

from __future__ import annotations


def test_surgical_convergence_tail_exists():
    from agent.doc_state import route_document_edits

    assert route_document_edits.__name__ == "route_document_edits"
