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
import pytest
from agent.edit_primitives import align_block_boundaries


def _apply(content: str, old: str, new: str) -> str:
    """Splice `old` → `new` through the alignment, as the resolver does."""
    from_cp = content.index(old)
    to_cp = from_cp + len(old)
    from_cp, to_cp, new_text = align_block_boundaries(content, from_cp, to_cp, new)
    return content[:from_cp] + new_text + content[to_cp:]


# ── The incident: heading glued to the previous paragraph ──

def test_heading_replacement_after_space_separator_gets_a_blank_line():
    content = "первый фрагмент речи второй фрагмент речи"
    out = _apply(content, "второй фрагмент речи", "## 2. Заголовок\n\nТекст.")

    assert out == "первый фрагмент речи\n\n## 2. Заголовок\n\nТекст."


def test_separator_whitespace_run_is_swallowed_not_left_dangling():
    """Two spaces (the doc-53e340da shape) must not survive as trailing whitespace
    before the inserted newlines."""
    content = "конец абзаца.  фрагмент"
    out = _apply(content, "фрагмент", "## 3. Заголовок\n\nТекст.")

    assert out == "конец абзаца.\n\n## 3. Заголовок\n\nТекст."


def test_existing_blank_line_is_left_alone():
    content = "конец абзаца.\n\nфрагмент"
    out = _apply(content, "фрагмент", "## 3. Заголовок\n\nТекст.")

    assert out == "конец абзаца.\n\n## 3. Заголовок\n\nТекст."


def test_single_existing_newline_is_not_widened():
    """Already a block boundary — the alignment adds nothing. Why: never reformat
    content the user (or an earlier edit) deliberately shaped."""
    content = "конец абзаца.\nфрагмент"
    out = _apply(content, "фрагмент", "## 3. Заголовок\n\nТекст.")

    assert out == "конец абзаца.\n## 3. Заголовок\n\nТекст."


def test_start_of_document_needs_no_padding():
    content = "фрагмент дальше"
    out = _apply(content, "фрагмент", "# Заголовок\n\nтекст")

    assert out == "# Заголовок\n\nтекст дальше"


# ── Right side: a block-ending replacement glued to following prose ──

def test_block_ending_replacement_gets_a_blank_line_before_following_prose():
    content = "фрагмент хвост абзаца"
    out = _apply(content, "фрагмент", "Текст.\n\n- пункт один")

    assert out == "Текст.\n\n- пункт один\n\nхвост абзаца"


def test_right_side_whitespace_run_swallowed():
    content = "фрагмент   хвост"
    out = _apply(content, "фрагмент", "- пункт")

    assert out == "- пункт\n\nхвост"


def test_end_of_document_needs_no_padding():
    content = "начало фрагмент"
    out = _apply(content, "фрагмент", "## Заголовок")

    assert out == "начало\n\n## Заголовок"


# ── Inline replacements are untouched (the guard against over-reach) ──

def test_plain_inline_replacement_is_untouched():
    content = "Мама мыла раму."
    out = _apply(content, "мыла", "красила")

    assert out == "Мама красила раму."


def test_multiline_prose_replacement_is_untouched():
    """Only BLOCK-construct edges align. Multi-line prose mid-sentence is the
    model's business — widening it would reformat authored text."""
    content = "Начало X конец"
    out = _apply(content, "X", "первая строка\n\nвторая строка")

    assert out == "Начало первая строка\n\nвторая строка конец"


@pytest.mark.parametrize("first_line", [
    "# H1", "###### H6", "- пункт", "* пункт", "+ пункт", "1. пункт",
    "> цитата", "```py", "| a | b |",
])
def test_every_block_construct_triggers_left_alignment(first_line):
    content = "хвост абзаца фрагмент"
    out = _apply(content, "фрагмент", first_line + "\nостальное")

    assert out.startswith("хвост абзаца\n\n" + first_line)


@pytest.mark.parametrize("not_block", ["#хэштег", "1.пункт", "-минус", "обычный текст"])
def test_non_block_first_lines_do_not_trigger_alignment(not_block):
    content = "хвост абзаца фрагмент"
    out = _apply(content, "фрагмент", not_block)

    assert out == "хвост абзаца " + not_block


def test_alignment_is_idempotent():
    content = "хвост абзаца фрагмент"
    once = _apply(content, "фрагмент", "## Заголовок")
    twice = _apply(once, "## Заголовок", "## Заголовок")

    assert once == twice
