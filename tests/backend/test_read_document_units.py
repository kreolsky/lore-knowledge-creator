"""Unit tests for read_document's decomposed helpers (R1 agent round 3/4).

`_read_document_exec` was a 210-line executor. The extracted phases are pinned
here at the unit level: the target resolve (not-found → soft error), the RBAC +
scope gate chain, the normalized read window, the base payload, and the
sidecar attach (tables / references).
"""

from unittest.mock import AsyncMock, patch

from agent.readonly_executors import (
    _read_gate_error,
)
from fastapi import HTTPException

# ─── _read_gate_error ────────────────────────────────────────────────────────


class TestReadGateError:


    async def test_scope_403_maps_to_its_detail(self):
        doc = {"project_id": "p1"}
        exc = HTTPException(status_code=403, detail="out of scope")
        with patch("agent.readonly_executors.get_document_access",
                   new_callable=AsyncMock, return_value="full"), \
             patch("scope.require_doc_in_scope",
                   new_callable=AsyncMock, side_effect=exc):
            err = await _read_gate_error(
                resolved="d1", doc=doc, project_id="p1",
                user={"user_id": "u"}, scope_root="root1",
            )
        assert err == "out of scope"
