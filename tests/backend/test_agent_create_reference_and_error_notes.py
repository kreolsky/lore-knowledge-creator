"""create_reference shared primitive + pipeline error-note refactor.

The proposal apply dispatch tests were deleted with the proposal cluster
(mid-turn approval replaced `_apply_one_proposal` / the apply executors). What
survives here: the create_reference_via_collab shared primitive (single source
for the Tool-API direct path + host invariant) and the pipeline error-note
delegation to the one `_make_pipeline_note` home.
"""
import inspect

import pytest


def test_shared_reference_primitive_is_single_source():
    """create_reference_via_collab exists in collab_writes (its owner since plan
    fewer-layers) and the Tool-API direct path delegates to it (no inline
    create_record/emit body left)."""
    from agent import collab_writes

    assert hasattr(collab_writes, "create_reference_via_collab")

    from routes.tool_api import _common as tool_api_common
    direct_src = inspect.getsource(tool_api_common._apply_create_reference_direct)
    assert "create_reference_via_collab" in direct_src
    assert "create_record" not in direct_src, (
        "inline create_record body removed from tool_api direct reference path"
    )

    prim_src = inspect.getsource(collab_writes.create_reference_via_collab)
    assert "create_reference_row" in prim_src, (
        "primitive delegates the row build + emit to the shared chokepoint"
    )
    assert "create_record" not in prim_src, (
        "no inline create_record in the primitive (service owns the row)"
    )


async def test_create_reference_primitive_rejects_missing_host():
    """Reference-host invariant: create_reference_via_collab raises a clear 400 when
    the host (document_id) is missing/empty, BEFORE any DB write — defense-in-depth
    for a path that bypasses the tool-layer host guards (e.g. a malformed
    create_reference proposal). Without it, an empty parent_id would trip the
    documents_reference_parent_check event as an opaque AppliedUnverifiedError."""
    from unittest.mock import AsyncMock, patch

    from agent.collab_writes import create_reference_via_collab
    from fastapi import HTTPException

    # The host check must fire BEFORE any DB write. The primitive delegates the row
    # build to create_reference_row (deferred import from documents.service), so patch
    # it at its source and assert it is never reached when the host is missing.
    with patch("documents.service.create_reference_row", new=AsyncMock()) as rec_mock:
        for bad in ("", None):
            with pytest.raises(HTTPException) as exc:
                await create_reference_via_collab(
                    document_id=bad, title="Ref", content="C",
                    media_type="markdown", source_url=None,
                    project_id="p1", user={"user_id": "u1"}, scope_root=None,
                )
            assert exc.value.status_code == 400
            assert "host" in exc.value.detail
    rec_mock.assert_not_called()


# ── pipeline note refactor (shared primitive) ─────────────────────────────────


def test_pipeline_create_error_note_uses_shared_primitive():
    """The extractor error-note path delegates persistence to the one note home."""
    from pipeline.extractor import runner

    src = inspect.getsource(runner._create_error_note)
    assert "_make_pipeline_note" in src  # the ONE home (create_system_note + emit)
    assert "create_record" not in src
