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


    def test_inverted_range_is_ignored(self):
        # Defensive: an inverted (from > to) payload must not corrupt the registry.
        reg.track("d1", "u1", "Alice", 30, 10)
        assert reg._selections.get("d1", {}).get("u1") is None


class TestConflicting:

    def test_own_selection_is_excluded(self):
        reg.track("d1", "u1", "Alice", 10, 20)
        assert reg.conflicting("d1", 15, 25, exclude_user_id="u1") is None


class TestExpiry:
    def test_stale_selection_does_not_block(self):
        import time

        reg.track("d1", "u2", "Bob", 10, 20)
        # Force the timestamp into the past beyond the TTL window.
        reg._selections["d1"]["u2"]["ts"] = time.monotonic() - (reg.SELECTION_TTL_SEC + 1)
        assert reg.conflicting("d1", 15, 25, exclude_user_id="u1") is None
