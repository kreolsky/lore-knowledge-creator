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
    RegionContainmentReject,
    _apply_resolved_edits,
    _classify_edit_miss,
    _reject_overlapping_edits,
    _resolve_one_edit,
    _validate_edits_against_snapshot,
)
from fastapi import HTTPException

CONTENT = "alpha bravo\ncharlie delta\necho foxtrot\n"


# ─── _classify_edit_miss ─────────────────────────────────────────────────────


class TestClassifyEditMiss:
    def test_already_applied_returns_skip_entry(self):
        skip = _classify_edit_miss(
            CONTENT, 0, "bravoXX", "charlie delta", "not_found",
        )
        assert skip == {"index": 0, "at_cp": 12, "reason": "already_applied"}

    def test_genuine_miss_raises_409_with_detail(self):
        with pytest.raises(HTTPException) as e:
            _classify_edit_miss(CONTENT, 2, "nope", "whatever", "not_found")
        assert e.value.status_code == 409
        assert e.value.detail["index"] == 2
        assert "error" in e.value.detail

    def test_full_rewrite_raises_422_not_409(self):
        with pytest.raises(HTTPException) as e:
            _classify_edit_miss(CONTENT, 0, CONTENT, "x", "full_rewrite")
        assert e.value.status_code == 422

    def test_ambiguous_raises_409(self):
        with pytest.raises(HTTPException) as e:
            _classify_edit_miss("x x x\n", 0, "x ", "y", "ambiguous")
        assert e.value.status_code == 409


# ─── _resolve_one_edit ───────────────────────────────────────────────────────


class TestResolveOneEdit:
    def test_plain_replacement_resolves(self):
        kind, entry = _resolve_one_edit(
            CONTENT, 1, {"old_string": "charlie", "new_string": "CHUCK"}, None,
            0.9,
        )
        assert kind == "resolved"
        assert entry["original_text"] == "charlie"
        assert entry["new_text"] == "CHUCK"
        assert CONTENT[entry["from_cp"]:entry["to_cp"]] == "charlie"

    def test_noop_edit_skips_as_already_applied(self):
        kind, entry = _resolve_one_edit(
            CONTENT, 0, {"old_string": "bravo", "new_string": "bravo"}, None,
            0.9,
        )
        assert kind == "skip"
        assert entry["reason"] == "already_applied"

    def test_region_violation_rejects(self):
        def region(from_cp, to_cp):
            return "outside pinned region" if from_cp < 5 else None

        with pytest.raises(RegionContainmentReject) as e:
            _resolve_one_edit(
                CONTENT, 0,
                {"old_string": "alpha", "new_string": "ALPHA"}, region, 0.9,
            )
        assert e.value.detail == "outside pinned region"

    def test_region_gate_passes_inside(self):
        def region(from_cp, to_cp):
            return None

        kind, _ = _resolve_one_edit(
            CONTENT, 0, {"old_string": "alpha", "new_string": "ALPHA"}, region, 0.9,
        )
        assert kind == "resolved"


# ─── _validate_edits_against_snapshot ────────────────────────────────────────


class TestValidateEditsAgainstSnapshot:
    def test_batch_splits_into_resolved_and_skipped(self):
        resolved, skipped = _validate_edits_against_snapshot(
            CONTENT,
            [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "bravoXX", "new_string": "bravo"},   # already there
                {"old_string": "echo", "new_string": "ECHO"},
            ],
            None, 0.9,
        )
        assert [r["index"] for r in resolved] == [0, 2]
        assert [s["index"] for s in skipped] == [1]

    def test_one_bad_edit_aborts_the_whole_batch(self):
        # new_string must be genuinely absent — the idempotent probe treats a
        # present-once new_string as "already applied" and skips instead of raising.
        with pytest.raises(HTTPException):
            _validate_edits_against_snapshot(
                CONTENT,
                [
                    {"old_string": "alpha", "new_string": "ALPHA"},
                    {"old_string": "miss", "new_string": "zebra-quite-absent"},
                ],
                None, 0.9,
            )


# ─── _reject_overlapping_edits ───────────────────────────────────────────────


class TestRejectOverlappingEdits:
    def test_overlapping_ranges_rejected_with_indices(self):
        resolved = [
            {"from_cp": 0, "to_cp": 12, "index": 0},
            {"from_cp": 6, "to_cp": 20, "index": 1},
        ]
        with pytest.raises(HTTPException) as e:
            _reject_overlapping_edits(resolved)
        assert e.value.status_code == 409
        assert e.value.detail["indices"] == [0, 1]

    def test_disjoint_ranges_pass(self):
        _reject_overlapping_edits([
            {"from_cp": 0, "to_cp": 5, "index": 0},
            {"from_cp": 10, "to_cp": 15, "index": 1},
        ])

    def test_single_edit_passes(self):
        _reject_overlapping_edits([{"from_cp": 3, "to_cp": 9, "index": 0}])


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
