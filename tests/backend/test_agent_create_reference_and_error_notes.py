"""create_reference shared primitive + pipeline error-note refactor.

The proposal apply dispatch tests were deleted with the proposal cluster
(mid-turn approval replaced `_apply_one_proposal` / the apply executors). What
survives here: the create_reference_via_collab shared primitive (single source
for the Tool-API direct path + host invariant) and the pipeline error-note
delegation to the one `_make_pipeline_note` home.
"""

import pytest


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
