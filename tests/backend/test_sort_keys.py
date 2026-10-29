"""Unit tests for the fractional sort-key utility (backend/sort_keys.py).

Ordering contract: keys are plain strings compared lexicographically. A single
drag must produce a key strictly between its neighbours without touching them.
"""

from sort_keys import n_keys_between


def test_n_keys_between_zero():
    assert n_keys_between(None, None, 0) == []
