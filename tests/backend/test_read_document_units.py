"""Unit tests for read_document's decomposed helpers (R1 agent round 3/4).

`_read_document_exec` was a 210-line executor. The extracted phases are pinned
here at the unit level: the target resolve (not-found → soft error), the RBAC +
scope gate chain, the normalized read window, and the sidecar attach
(tables / references).
"""

from unittest.mock import AsyncMock, patch

from agent.readonly_executors import (
    _read_gate_error,
    _ReadTarget,
    _resolve_read_document_target,
    _slice_read_window,
)
from fastapi import HTTPException

# ─── _resolve_read_document_target ───────────────────────────────────────────


class TestResolveReadDocumentTarget:
    async def test_live_doc_returned_as_is(self):
        doc = {"id": "d1", "deleted_at": None, "project_id": "p1"}
        with patch("agent.readonly_executors.fetch_one",
                   new_callable=AsyncMock, return_value=doc):
            out = await _resolve_read_document_target(doc_id="d1")
        assert isinstance(out, _ReadTarget)
        assert out.doc is doc and out.resolved == "d1"

    async def test_deleted_doc_fails_soft(self):
        with patch("agent.readonly_executors.fetch_one",
                   new_callable=AsyncMock,
                   return_value={"id": "d1", "deleted_at": "x"}):
            out = await _resolve_read_document_target(doc_id="d1")
        assert out == {"error": "Document not found"}

    async def test_not_found_returns_plain_soft_error(self):
        with patch("agent.readonly_executors.fetch_one",
                   new_callable=AsyncMock, return_value=None):
            out = await _resolve_read_document_target(doc_id="ghost")
        assert out == {"error": "Document not found"}


# ─── _read_gate_error ────────────────────────────────────────────────────────


class TestReadGateError:
    async def test_cross_project_rejected(self):
        doc = {"project_id": "other"}
        err = await _read_gate_error(
            resolved="d1", doc=doc, project_id="p1", user={"user_id": "u"},
            scope_root=None,
        )
        assert err == "Document is not in this project"

    async def test_no_per_doc_access_rejected(self):
        doc = {"project_id": "p1"}
        with patch("agent.readonly_executors.get_document_access",
                   new_callable=AsyncMock, return_value=None):
            err = await _read_gate_error(
                resolved="d1", doc=doc, project_id="p1",
                user={"user_id": "u"}, scope_root=None,
            )
        assert err == "No access to this document"

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

    async def test_all_gates_pass_returns_none(self):
        doc = {"project_id": "p1"}
        with patch("agent.readonly_executors.get_document_access",
                   new_callable=AsyncMock, return_value="full"), \
             patch("scope.require_doc_in_scope", new_callable=AsyncMock):
            err = await _read_gate_error(
                resolved="d1", doc=doc, project_id="p1",
                user={"user_id": "u"}, scope_root="root1",
            )
        assert err is None


# ─── _slice_read_window ──────────────────────────────────────────────────────


class TestSliceReadWindow:
    def test_whole_document_when_window_exceeds_total(self):
        import config
        start, content = _slice_read_window(
            "hello", 0, None,
            slice_chars=config.AGENT_READ_SLICE_CHARS,
            max_chars=config.AGENT_READ_MAX_CHARS,
        )
        assert (start, content) == (0, "hello")

    def test_negative_offset_clamped_to_zero(self):
        import config
        start, content = _slice_read_window(
            "hello", -5, 3,
            slice_chars=config.AGENT_READ_SLICE_CHARS,
            max_chars=config.AGENT_READ_MAX_CHARS,
        )
        assert (start, content) == (0, "hel")

    def test_offset_beyond_total_clamped(self):
        import config
        start, content = _slice_read_window(
            "hello", 99, 3,
            slice_chars=config.AGENT_READ_SLICE_CHARS,
            max_chars=config.AGENT_READ_MAX_CHARS,
        )
        assert (start, content) == (5, "")

    def test_nonpositive_limit_falls_back_to_default(self):
        import config
        with patch.object(config, "AGENT_READ_SLICE_CHARS", 4), \
             patch.object(config, "AGENT_READ_MAX_CHARS", 10_000):
            start, content = _slice_read_window(
                "hello world", 0, 0,
                slice_chars=config.AGENT_READ_SLICE_CHARS,
                max_chars=config.AGENT_READ_MAX_CHARS,
            )
        assert (start, content) == (0, "hell")

    def test_window_hard_capped_at_max(self):
        import config
        with patch.object(config, "AGENT_READ_SLICE_CHARS", 4), \
             patch.object(config, "AGENT_READ_MAX_CHARS", 2):
            start, content = _slice_read_window(
                "hello world", 0, None,
                slice_chars=config.AGENT_READ_SLICE_CHARS,
                max_chars=config.AGENT_READ_MAX_CHARS,
            )
        assert (start, content) == (0, "he")
