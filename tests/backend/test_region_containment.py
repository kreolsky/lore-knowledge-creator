"""B2: pinned-region containment enforcement (hard gate).

# SYSTEM: chat-region-containment-tests — REGION_OUT_OF_SCOPE rejection at apply.

The authoritative choke point is `region_containment_error` (relocated to
`agent.apply_edits_resolver`): given a resolved edit range
(edit_from, edit_to) and the session's pinned region, the edit MUST lie fully
inside the region (same doc_id). A violation returns a detail string the caller
maps to REGION_OUT_OF_SCOPE. Fail-closed: pinned but no region in the request →
reject.
"""


# ─── pure containment helper ──────────────────────────────────────────────────


def test_containment_inside_region_is_ok():
    from agent.apply_edits_resolver import region_containment_error

    err = region_containment_error(
        edit_from=7, edit_to=10, edit_doc_id="d-1",
        has_region=True,
        region={"doc_id": "d-1", "from_cp": 6, "to_cp": 11},
    )
    assert err is None


def test_containment_outside_region_is_violation():
    from agent.apply_edits_resolver import region_containment_error

    err = region_containment_error(
        edit_from=6, edit_to=11, edit_doc_id="d-1",
        has_region=True,
        region={"doc_id": "d-1", "from_cp": 0, "to_cp": 5},
    )
    assert err is not None
    assert "outside" in err.lower() or "out of" in err.lower()


def test_containment_partial_overlap_is_violation():
    from agent.apply_edits_resolver import region_containment_error

    # Edit [6,11) vs region [8,12): overlaps but is not fully inside.
    err = region_containment_error(
        edit_from=6, edit_to=11, edit_doc_id="d-1",
        has_region=True,
        region={"doc_id": "d-1", "from_cp": 8, "to_cp": 12},
    )
    assert err is not None


def test_containment_wrong_doc_is_violation():
    from agent.apply_edits_resolver import region_containment_error

    err = region_containment_error(
        edit_from=0, edit_to=3, edit_doc_id="d-1",
        has_region=True,
        region={"doc_id": "d-OTHER", "from_cp": 0, "to_cp": 10},
    )
    assert err is not None


def test_containment_fail_closed_when_pinned_but_no_region():
    from agent.apply_edits_resolver import region_containment_error

    err = region_containment_error(
        edit_from=0, edit_to=3, edit_doc_id="d-1",
        has_region=True, region=None,
    )
    assert err is not None


def test_containment_no_region_flag_means_unconstrained():
    from agent.apply_edits_resolver import region_containment_error

    # Not pinned → no constraint, regardless of region presence.
    err = region_containment_error(
        edit_from=0, edit_to=3, edit_doc_id="d-1",
        has_region=False, region=None,
    )
    assert err is None

