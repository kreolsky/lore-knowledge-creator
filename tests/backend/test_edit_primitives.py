"""`resolve_edit_range` normalized-fold matching (plan:
normalized-fold-edit-matching). The model cannot perceive invisible characters
(trailing spaces, NBSP, \r\n, zero-width) even in verbatim content, so it emits
a clean projection while the document holds the real chars and the exact
`content.find` permanently fails. The resolver folds BOTH sides to a normalized
projection (with an offset map back to original code points), searches the
projection, and splices the ORIGINAL range — but only when the projected match
is UNIQUE (fold relaxes characters, never uniqueness).

The old JSON.stringify escaped-`\\n` retry is subsumed by the fold (old_string
is de-escaped before folding).
"""
import pytest
from agent.edit_primitives import resolve_edit_range

from config import AGENT_FULL_REWRITE_FRACTION

# ── Fold: escaped-control-char cases (subsumed from the old de-escape retry) ──

def test_literal_escaped_newline_old_string_resolves_against_real_newline_content():
    content = "line one\nline two\nline three"
    old_string = "line one\\nline two"  # literal backslash-n, as JSON.stringify would emit it

    assert resolve_edit_range(content, old_string, full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION) == (0, len("line one\nline two"), True)


def test_fold_retry_reports_flag_true():
    """Contract: the resolver signals the fold retry fired so callers apply the
    SAME fold to new_string (old/new folded together or not at all)."""
    content = "line one\nline two\nline three"
    rng = resolve_edit_range(content, "line one\\nline two", full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION)
    assert rng[2] is True


def test_exact_match_reports_flag_false():
    content = "Hello cruel world"
    assert resolve_edit_range(content, "cruel", full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION) == (6, 11, False)


def test_folded_candidate_matching_multiple_times_is_ambiguous():
    """Fold relaxes characters, never uniqueness: >1 projected matches → ambiguous
    (the model must add surrounding context), never a silently-promoted range."""
    content = "a\nb\na\nb"
    assert resolve_edit_range(content, "a\\nb", full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION) == "ambiguous"


def test_fold_retry_still_not_found_with_no_match():
    content = "completely different content"
    assert resolve_edit_range(content, "missing\\nline", full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION) == "not_found"


def test_literal_tab_and_carriage_return_also_folded():
    content = "prefix a\tb\rc suffix padding to avoid the full-rewrite guard"
    old_string = "a\\tb\\rc"
    assert resolve_edit_range(content, old_string, full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION) == (7, 7 + len("a\tb\rc"), True)


def test_genuine_ambiguous_without_escapes_unaffected():
    assert resolve_edit_range("dup dup dup", "dup", full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION) == "ambiguous"


def test_genuine_not_found_without_escapes_unaffected():
    assert resolve_edit_range("hello world", "goodbye", full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION) == "not_found"


def test_exact_match_takes_priority_over_fold_retry():
    """A doc that legitimately contains the literal text `\\n` (e.g. code/regex)
    must match on the raw string first — the fold retry only fires when the raw
    `content.find` fails, so the flag is False (no fold applied)."""
    content = "line one\\nline two\nline three, more padding to avoid the full-rewrite guard"
    old_string = "line one\\nline two"
    assert resolve_edit_range(content, old_string, full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION) == (0, len(old_string), False)


# ── Fold: invisible-character cases (the new relaxation) ──

def test_trailing_space_before_newline_folds_and_range_absorbs_the_space():
    """The doc's separators are `--- ` (trailing space); the model sends `---`.
    The fold match resolves and the ORIGINAL range covers the trailing space so
    it is removed with the separator."""
    content = "a\n--- \n\nb"
    old_string = "a\n---\n\nb"
    rng = resolve_edit_range(content, old_string, full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION)
    assert not isinstance(rng, str)
    from_cp, to_cp, folded = rng
    assert folded is True
    # Range covers the whole content, INCLUDING the trailing space at index 5.
    assert content[from_cp:to_cp] == content
    assert " \n" in content[from_cp:to_cp]  # the invisible trailing space is inside the range


def test_trailing_space_at_end_of_string_not_absorbed():
    """EOS trailing spaces are stripped in the projection, so old_string ending at
    'me' matches — and the ORIGINAL range ends at 'me', NOT absorbing the spaces
    (boundary rule: only whitespace BETWEEN matched chars is consumed)."""
    content = "keep this\ndelete me   "  # trailing spaces at EOS
    old_string = "delete me"
    rng = resolve_edit_range(content, old_string, full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION)
    assert not isinstance(rng, str)
    from_cp, to_cp, _folded = rng
    assert content[from_cp:to_cp] == "delete me"  # trailing spaces NOT absorbed


def test_nbsp_content_vs_plain_space_old_string_folds():
    content = "before   after padding to avoid full-rewrite"  # NBSP in content
    old_string = "before   after"  # plain spaces
    # NBSP folds to a space; the two projections match.
    rng = resolve_edit_range(content, old_string, full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION)
    assert not isinstance(rng, str)
    assert rng[2] is True
    assert content[rng[0]:rng[1]] == "before   after"


def test_narrow_nbsp_folds_like_nbsp():
    content = "цена 100 рублей padding to avoid the full rewrite guard"
    old_string = "цена 100 рублей"
    rng = resolve_edit_range(content, old_string, full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION)
    assert not isinstance(rng, str)
    assert content[rng[0]:rng[1]] == "цена 100 рублей"


def test_crlf_content_vs_lf_old_string_folds():
    content = "one\r\ntwo\r\nthree padding padding padding padding"
    old_string = "one\ntwo"
    rng = resolve_edit_range(content, old_string, full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION)
    assert not isinstance(rng, str)
    assert rng[2] is True
    assert content[rng[0]:rng[1]] == "one\r\ntwo"


def test_zero_width_chars_dropped_in_fold():
    content = "wo​rd padding to avoid the full-rewrite guard entirely"
    old_string = "word"
    rng = resolve_edit_range(content, old_string, full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION)
    assert not isinstance(rng, str)
    assert content[rng[0]:rng[1]] == "wo​rd"


@pytest.mark.xfail(
    reason="NFC combining across code points needs segment-level offset mapping; "
    "deferred (plan §Offset-map mechanics). Per-char NFC only.",
    strict=True,
)
def test_nfd_content_vs_nfc_old_string_folds():
    content = "café padding to avoid the full-rewrite guard entirely here"  # NFD: e + combining acute
    old_string = "café"  # NFC: precomposed é
    rng = resolve_edit_range(content, old_string, full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION)
    assert not isinstance(rng, str)


def test_exact_match_with_trailing_space_in_old_string_uses_raw_path():
    """old_string legitimately containing the trailing space matches raw first
    (flag False) — the fold only fires on a raw miss."""
    content = "a\n--- \n\nb padding to avoid the full-rewrite guard entirely here"
    old_string = "--- "
    rng = resolve_edit_range(content, old_string, full_rewrite_fraction=AGENT_FULL_REWRITE_FRACTION)
    assert rng == (2, 6, False)
