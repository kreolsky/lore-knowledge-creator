"""Unit tests for the fractional sort-key utility (backend/sort_keys.py).

Ordering contract: keys are plain strings compared lexicographically. A single
drag must produce a key strictly between its neighbours without touching them.
"""

import pytest
from sort_keys import key_before, key_between, n_keys_between


def test_key_between_none_none_is_stable():
    k = key_between(None, None)
    assert isinstance(k, str) and k


def test_key_before_sorts_before_existing():
    first = key_between(None, None)
    before = key_before(first)
    assert before < first


def test_key_between_is_strictly_between():
    a = key_between(None, None)
    b = key_between(a, None)
    mid = key_between(a, b)
    assert a < mid < b


def test_repeated_midpoints_stay_ordered():
    a = key_between(None, None)
    b = key_between(a, None)
    lo, hi = a, b
    keys = []
    for _ in range(40):
        mid = key_between(lo, hi)
        assert lo < mid < hi
        keys.append(mid)
        hi = mid  # keep inserting toward the low end
    # Strictly decreasing sequence, all distinct
    assert keys == sorted(keys, reverse=True)
    assert len(set(keys)) == len(keys)


def test_key_before_repeated_prepend_stays_ordered():
    """Newest-first create: each new doc prepends above the previous top."""
    top = key_between(None, None)
    tops = [top]
    for _ in range(20):
        top = key_before(top)
        assert top < tops[-1]
        tops.append(top)


def test_n_keys_between_returns_ordered_distinct_keys():
    keys = n_keys_between(None, None, 5)
    assert len(keys) == 5
    assert keys == sorted(keys)
    assert len(set(keys)) == 5


def test_n_keys_between_zero():
    assert n_keys_between(None, None, 0) == []


def test_key_between_rejects_inverted_bounds():
    a = key_between(None, None)
    b = key_between(a, None)
    with pytest.raises(Exception):
        key_between(b, a)  # b > a → invalid order
