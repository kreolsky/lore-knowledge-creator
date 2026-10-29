"""Block-boundary alignment for agent splices (incident doc-53e340da).

A batch edit over a speech transcript replaced consecutive fragments that were
separated in the ORIGINAL text by a plain space, never a newline. Each new_string
carried correct `\\n\\n` internally, but the separator BETWEEN two replaced ranges
survived as-is, so every rewritten section heading landed glued to the tail of the
previous paragraph:

    ...формулировать идеальный текст. ## 2. Удобное редактирование

The class is not "the model forgot a newline": a replacement whose first (or last)
line is a BLOCK construct cannot render correctly unless its range starts (ends) on
a block boundary. `align_block_boundaries` is the projection over that class.
"""
from agent.edit_primitives import align_block_boundaries


def _apply(content: str, old: str, new: str) -> str:
    """Splice `old` → `new` through the alignment, as the resolver does."""
    from_cp = content.index(old)
    to_cp = from_cp + len(old)
    from_cp, to_cp, new_text = align_block_boundaries(content, from_cp, to_cp, new)
    return content[:from_cp] + new_text + content[to_cp:]


# ── The incident: heading glued to the previous paragraph ──


def test_separator_whitespace_run_is_swallowed_not_left_dangling():
    """Two spaces (the doc-53e340da shape) must not survive as trailing whitespace
    before the inserted newlines."""
    content = "конец абзаца.  фрагмент"
    out = _apply(content, "фрагмент", "## 3. Заголовок\n\nТекст.")

    assert out == "конец абзаца.\n\n## 3. Заголовок\n\nТекст."


# ── Right side: a block-ending replacement glued to following prose ──


def test_right_side_whitespace_run_swallowed():
    content = "фрагмент   хвост"
    out = _apply(content, "фрагмент", "- пункт")

    assert out == "- пункт\n\nхвост"
