"""Unit tests for the advisory selection-conflict pre-apply check (plan stage-1 §3.6).

# INVARIANT (audit R2): selection-conflict NEVER overrides CRDT convergence. It
# is a best-effort, NON-atomic pre-apply UX check that races the CRDT write by
# design; the authoritative last line stays `_apply_edit_proposal` /
# `apply_edit_to_document` → `merge_live_content`. selection_conflict_check must
# NOT mutate anything — it only reads live content + the in-memory selection
# registry and reports a conflict (or None).
"""

import pytest
from collab import selection_registry as reg
from routes.chat import selection_conflict


@pytest.fixture(autouse=True)
def _reset_registry():
    reg._reset()
    yield
    reg._reset()


class TestSelectionConflictCheck:
    async def test_no_selection_no_conflict(self):
        result = await selection_conflict.selection_conflict_check(
            "d1", "beta", exclude_user_id="u-agent", content="alpha beta gamma",
        )
        assert result is None

    async def test_overlapping_selection_is_a_conflict(self):
        # "beta" lives at code-point offsets [6,10) in "alpha beta gamma".
        reg.track("d1", "u-human", "Human", 8, 12)
        result = await selection_conflict.selection_conflict_check(
            "d1", "beta", exclude_user_id="u-agent", content="alpha beta gamma",
        )
        assert result is not None
        assert result["user_id"] == "u-human"
        assert result["user_name"] == "Human"

    async def test_disjoint_selection_is_not_a_conflict(self):
        reg.track("d1", "u-human", "Human", 0, 3)  # covers "alpha", not "beta"
        result = await selection_conflict.selection_conflict_check(
            "d1", "beta", exclude_user_id="u-agent", content="alpha beta gamma",
        )
        assert result is None

    async def test_unresolvable_old_string_returns_none(self):
        # A not_found / ambiguous / full_rewrite range cannot be conflict-rejected:
        # the apply will itself reject it; selection-conflict must not preempt that
        # path.
        result = await selection_conflict.selection_conflict_check(
            "d1", "nope", exclude_user_id="u-agent", content="alpha beta gamma",
        )
        assert result is None

    async def test_does_not_mutate_registry(self):
        reg.track("d1", "u-human", "Human", 8, 12)
        before = dict(reg._selections.get("d1", {}))
        await selection_conflict.selection_conflict_check(
            "d1", "beta", exclude_user_id="u-agent", content="alpha beta gamma",
        )
        after = reg._selections.get("d1", {})
        assert before.keys() == after.keys()
        assert (after["u-human"]["from_cp"], after["u-human"]["to_cp"]) == (8, 12)

    async def test_stale_selection_does_not_block(self):
        import time

        reg.track("d1", "u-human", "Human", 8, 12)
        reg._selections["d1"]["u-human"]["ts"] = time.monotonic() - (reg.SELECTION_TTL_SEC + 1)
        result = await selection_conflict.selection_conflict_check(
            "d1", "beta", exclude_user_id="u-agent", content="alpha beta gamma",
        )
        assert result is None
