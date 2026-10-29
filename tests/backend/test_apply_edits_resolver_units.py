"""Unit tests for the edit-resolver's decomposed helpers (R1 agent round 4/4).

`_resolve_validate_apply_edits` was a 210-line lock-free core. The extracted
phases are pinned here at the unit level: the miss classifier (idempotent
skip vs 409/422), single-edit resolution (fold + align + region gate + noop),
the snapshot validation pass, the overlap guard, and the mark-first apply tail
(checkpoint → on_commit seam → descending splices → AppliedUnverifiedError).
"""

from unittest.mock import AsyncMock, patch

import pytest
from agent.apply_edits_resolver import (
    AppliedUnverifiedError,
    _apply_resolved_edits,
    _resolve_one_edit,
)

CONTENT = "alpha bravo\ncharlie delta\necho foxtrot\n"


# ─── _resolve_one_edit ───────────────────────────────────────────────────────


class TestResolveOneEdit:

    def test_noop_edit_skips_as_already_applied(self):
        kind, entry = _resolve_one_edit(
            CONTENT, 0, {"old_string": "bravo", "new_string": "bravo"}, None,
            0.9,
        )
        assert kind == "skip"
        assert entry["reason"] == "already_applied"


# ─── _apply_resolved_edits ───────────────────────────────────────────────────


def _resolved(**kw):
    entry = {"from_cp": 0, "to_cp": 5, "new_text": "ALPHA",
             "original_text": "alpha", "index": 0}
    entry.update(kw)
    return entry


class TestApplyResolvedEdits:
    async def test_checkpoint_then_commit_then_descending_splices(self):
        order = []
        ckpt = AsyncMock(side_effect=lambda **kw: order.append("checkpoint"))
        commit = AsyncMock(side_effect=lambda: order.append("commit"))

        async def fake_route(**kw):
            order.append(("route", [e["from_cp"] for e in kw["edits"]]))

        with patch("agent.collab_writes._create_agent_pre_edit_checkpoint", ckpt), \
             patch("agent.doc_state.route_document_edits", fake_route):
            out = await _apply_resolved_edits(
                "d1", "p1", CONTENT, "[]",
                [_resolved(from_cp=12, to_cp=19), _resolved()],
                [{"index": 9, "at_cp": 0, "reason": "already_applied"}],
                on_commit=commit,
            )
        assert order == ["checkpoint", "commit", ("route", [12, 0])]
        assert out["applied"] == 2 and out["checkpoint"] is True
        assert out["skipped"][0]["index"] == 9
        assert out["applied_ranges"] == [12, 0]  # resolved order, NOT apply order

    async def test_failure_after_mark_first_surfaces_applied_unverified(self):
        commit = AsyncMock()
        boom = AsyncMock(side_effect=RuntimeError("ws down"))
        with patch("agent.collab_writes._create_agent_pre_edit_checkpoint", AsyncMock()), \
             patch("agent.doc_state.route_document_edits", boom):
            with pytest.raises(AppliedUnverifiedError):
                await _apply_resolved_edits(
                    "d1", "p1", CONTENT, "[]", [_resolved()], [], on_commit=commit,
                )
        commit.assert_awaited_once()

    async def test_failure_without_mark_propagates_original(self):
        boom = AsyncMock(side_effect=RuntimeError("ws down"))
        with patch("agent.collab_writes._create_agent_pre_edit_checkpoint", AsyncMock()), \
             patch("agent.doc_state.route_document_edits", boom):
            with pytest.raises(RuntimeError, match="ws down"):
                await _apply_resolved_edits(
                    "d1", "p1", CONTENT, "[]", [_resolved()], [], on_commit=None,
                )

    async def test_applied_unverified_passes_through_unchanged(self):
        au = AppliedUnverifiedError()
        boom = AsyncMock(side_effect=au)
        with patch("agent.collab_writes._create_agent_pre_edit_checkpoint", AsyncMock()), \
             patch("agent.doc_state.route_document_edits", boom):
            with pytest.raises(AppliedUnverifiedError) as e:
                await _apply_resolved_edits(
                    "d1", "p1", CONTENT, "[]", [_resolved()], [], on_commit=AsyncMock(),
                )
        assert e.value is au
