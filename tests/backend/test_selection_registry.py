"""Unit tests for the advisory region-lock selection registry (plan stage-1 §3.6).

The registry holds in-memory, per-document active selections reported by collab
clients over the WS channel. Region-lock is ADVISORY: it only feeds a best-effort
pre-apply UX check; the authoritative convergence line stays the CRDT re-resolve.
These tests are pure-unit (no DB / no collab session).
"""

import pytest
from collab import selection_registry as reg


@pytest.fixture(autouse=True)
def _reset_registry():
    reg._reset()
    yield
    reg._reset()


class TestTrack:
    def test_non_collapsed_range_is_stored(self):
        reg.track("d1", "u1", "Alice", 10, 20)
        sel = reg._selections.get("d1", {}).get("u1")
        assert sel is not None
        assert (sel["from_cp"], sel["to_cp"]) == (10, 20)
        assert sel["user_name"] == "Alice"

    def test_collapsed_range_is_treated_as_no_selection(self):
        # A bare caret (from == to) must NOT block edits — register it as absent.
        reg.track("d1", "u1", "Alice", 5, 5)
        assert reg._selections.get("d1", {}).get("u1") is None

    def test_collapsed_after_range_clears(self):
        reg.track("d1", "u1", "Alice", 10, 20)
        reg.track("d1", "u1", "Alice", 12, 12)
        assert reg._selections.get("d1", {}).get("u1") is None

    def test_update_replaces_previous_range(self):
        reg.track("d1", "u1", "Alice", 10, 20)
        reg.track("d1", "u1", "Alice", 40, 60)
        sel = reg._selections["d1"]["u1"]
        assert (sel["from_cp"], sel["to_cp"]) == (40, 60)

    def test_clear_removes(self):
        reg.track("d1", "u1", "Alice", 10, 20)
        reg.clear("d1", "u1")
        assert reg._selections.get("d1", {}).get("u1") is None

    def test_inverted_range_is_ignored(self):
        # Defensive: an inverted (from > to) payload must not corrupt the registry.
        reg.track("d1", "u1", "Alice", 30, 10)
        assert reg._selections.get("d1", {}).get("u1") is None


class TestConflicting:
    def test_overlapping_range_of_other_user_is_returned(self):
        reg.track("d1", "u2", "Bob", 10, 20)
        c = reg.conflicting("d1", 15, 25, exclude_user_id="u1")
        assert c is not None
        assert c["user_id"] == "u2"
        assert c["user_name"] == "Bob"

    def test_own_selection_is_excluded(self):
        reg.track("d1", "u1", "Alice", 10, 20)
        assert reg.conflicting("d1", 15, 25, exclude_user_id="u1") is None

    def test_disjoint_range_is_not_a_conflict(self):
        reg.track("d1", "u2", "Bob", 0, 5)
        assert reg.conflicting("d1", 10, 20, exclude_user_id="u1") is None

    def test_adjacent_ranges_do_not_overlap(self):
        # [0,5) and [5,10) share only a boundary — not an intersection.
        reg.track("d1", "u2", "Bob", 0, 5)
        assert reg.conflicting("d1", 5, 10, exclude_user_id="u1") is None

    def test_fully_contained_range_is_a_conflict(self):
        reg.track("d1", "u2", "Bob", 0, 100)
        assert reg.conflicting("d1", 10, 20, exclude_user_id="u1") is not None

    def test_no_selections_returns_none(self):
        assert reg.conflicting("d1", 0, 10, exclude_user_id="u1") is None

    def test_unknown_doc_returns_none(self):
        assert reg.conflicting("missing", 0, 10, exclude_user_id="u1") is None

    def test_multiple_users_returns_a_conflict(self):
        reg.track("d1", "u2", "Bob", 0, 5)
        reg.track("d1", "u3", "Cara", 100, 110)
        c = reg.conflicting("d1", 102, 108, exclude_user_id="u1")
        assert c is not None
        assert c["user_id"] == "u3"


class TestExpiry:
    def test_stale_selection_does_not_block(self):
        import time

        reg.track("d1", "u2", "Bob", 10, 20)
        # Force the timestamp into the past beyond the TTL window.
        reg._selections["d1"]["u2"]["ts"] = time.monotonic() - (reg.SELECTION_TTL_SEC + 1)
        assert reg.conflicting("d1", 15, 25, exclude_user_id="u1") is None

    def test_fresh_selection_blocks(self):
        reg.track("d1", "u2", "Bob", 10, 20)
        assert reg.conflicting("d1", 15, 25, exclude_user_id="u1") is not None


class TestRangesOverlap:
    @pytest.mark.parametrize("a,b,expected", [
        ((0, 5), (5, 10), False),     # adjacent
        ((0, 5), (6, 10), False),     # disjoint
        ((0, 5), (4, 10), True),      # partial overlap
        ((0, 10), (2, 4), True),      # contained
        ((2, 4), (0, 10), True),      # containing
        ((0, 5), (0, 5), True),       # identical
    ])
    def test_overlap(self, a, b, expected):
        assert reg.ranges_overlap(a[0], a[1], b[0], b[1]) is expected
