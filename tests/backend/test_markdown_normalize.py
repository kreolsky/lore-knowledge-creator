"""Tests for markdown_normalize: dedent list-nested fences + reflow wrapped bullets.

The normalizer runs at reference-upload time so agent-written Markdown renders
cleanly in Lore's decoration-based CM6 editor (which mangles list-nested fenced
code and soft-wrapped bullet lines). See plan: normalize agent-written Markdown.
"""


from markdown_normalize import (
    normalize_markdown,
    unwrap_placeholder_brackets,
)


def test_idempotence():
    src = (
        "- bullet wrapping onto\n"
        "  a second line\n"
        "\n"
        "  ```yaml\n"
        "  key: value\n"
        "  ```\n"
        "\n"
        "- another bullet\n"
        "  - real child\n"
    )
    once = normalize_markdown(src)
    twice = normalize_markdown(once)
    assert once == twice


# ── Blockquote reflow (prose-with-prefix) ─────────────────────────────────────


def test_blockquote_empty_line_is_separator():
    # A bare `>` line is a paragraph break *inside* the quote and must survive.
    src = (
        "> First quoted paragraph line that is long enough to be treated as wrapped ok\n"
        ">\n"
        "> Second quoted paragraph after the empty quote line that stays as a separator\n"
    )
    out = normalize_markdown(src)
    assert "\n>\n" in out
    assert out.count("> First") == 1
    assert out.count("> Second") == 1


def test_inline_code_pipe_is_not_a_table():
    # A `|` inside an inline-code span must not be mistaken for a table row — the
    # quote should still fold across it.
    src = (
        "> Feature line mentioning `kind: multiselect | prefix | open` near the wrap col\n"
        "> and continuing on a second quoted line that should fold into one quote line.\n"
    )
    out = normalize_markdown(src)
    assert out.strip().count("\n") == 0
    assert "| prefix | open`" in out and "fold into one quote line." in out


# ── List/task spacing normalization ─────────────────────────────────────────
# LLMs emit task-list / bullet markdown with an irregular run of spaces after the
# marker — e.g. `*   [ ]` (three spaces) — which breaks Lore's CM6 flicker guard
# and renders poorly. normalize_list_spacing collapses the after-marker (and
# after-task-marker) run of spaces to a single space, preserving leading indent.


def test_collapse_blank_between_numbered_items():
    # The exact user example: docx/Pandoc loose numbered list → tight on import.
    src = (
        "1. Точка входа в функционал глубоко спрятана, недоступно новичкам\n"
        "\n"
        "2. Монетизирующая фича не промотируется\n"
    )
    out = normalize_markdown(src)
    assert "\n\n" not in out.strip()
    assert (
        out
        == "1. Точка входа в функционал глубоко спрятана, недоступно новичкам\n"
        "2. Монетизирующая фича не промотируется\n"
    )


def test_collapse_blank_between_bullet_items():
    for marker in ("-", "*", "+"):
        src = f"{marker} first\n\n{marker} second\n\n{marker} third\n"
        out = normalize_markdown(src)
        assert out == f"{marker} first\n{marker} second\n{marker} third\n"


def test_collapse_blank_between_nested_items():
    src = (
        "- top one\n"
        "\n"
        "  - nested one\n"
        "\n"
        "  - nested two\n"
        "\n"
        "- top two\n"
    )
    out = normalize_markdown(src)
    assert out == (
        "- top one\n"
        "  - nested one\n"
        "  - nested two\n"
        "- top two\n"
    )


def test_keep_blank_before_paragraph_after_list():
    src = "- item one\n- item two\n\nSome prose paragraph.\n"
    out = normalize_markdown(src)
    assert out == src


def test_keep_blank_before_heading_after_list():
    src = "- item one\n- item two\n\n## Heading\n"
    out = normalize_markdown(src)
    assert out == src


def test_keep_blank_inside_fence():
    src = (
        "```\n"
        "- looks like item\n"
        "\n"
        "- another\n"
        "```\n"
    )
    out = normalize_markdown(src)
    assert out == src


def test_keep_blank_before_item_continuation_paragraph():
    # Multi-paragraph list item: blank + indented continuation prose must survive.
    src = (
        "- item one\n"
        "\n"
        "  continuation paragraph of item one\n"
    )
    out = normalize_markdown(src)
    assert "\n\n  continuation paragraph" in out


def test_collapse_list_blanks_idempotent():
    src = "1. a\n\n2. b\n\n3. c\n"
    once = normalize_markdown(src)
    twice = normalize_markdown(once)
    assert once == twice


# ── unwrap_placeholder_brackets (agent write-side; plan agent-link-form-angle-brackets)
#
# The agent's instructions spell link slots as `[text](<id>)`; a weak model copies
# the placeholder brackets verbatim into `[text](<uuid>)`, which fails SAFE_ID_RE
# in extract_doc_mentions and renders broken. The unwrap strips the brackets at
# the agent write sites.


def test_unwrap_bare_document_id():
    out = unwrap_placeholder_brackets("[text](<526bf4e3-f775-439a-a2b0-51d50fb9bdb7>)")
    assert out == "[text](526bf4e3-f775-439a-a2b0-51d50fb9bdb7)"


def test_unwrap_ref_image_form():
    out = unwrap_placeholder_brackets("![alt|800x600](<ref:abc-123>)")
    assert out == "![alt|800x600](ref:abc-123)"


def test_unwrap_table_form():
    out = unwrap_placeholder_brackets("![Table](<table:tbl_1>)")
    assert out == "![Table](table:tbl_1)"


def test_unwrap_leaves_autolink_destination_untouched():
    # `<https://…>` is a legal CommonMark destination where the brackets are MEANT.
    src = "[site](<https://example.com/a?b=1>) and [doc](<526bf4e3-f775>)"
    out = unwrap_placeholder_brackets(src)
    assert "[site](<https://example.com/a?b=1>)" in out
    assert "[doc](526bf4e3-f775)" in out


def test_unwrap_leaves_bare_autolink_untouched():
    # A bare `<https://…>` autolink outside any link never matches.
    src = "see <https://example.com> for [doc](<abc-def>)"
    out = unwrap_placeholder_brackets(src)
    assert "<https://example.com>" in out
    assert "[doc](abc-def)" in out


def test_unwrap_inside_fence_untouched():
    # Prose ABOUT the syntax inside a fenced block must survive verbatim.
    src = "```\n[text](<526bf4e3-f775>)\n```\n"
    assert unwrap_placeholder_brackets(src) == src


def test_unwrap_inside_inline_code_span_untouched():
    src = "write `[text](<526bf4e3-f775>)` in prose, but [real](<abc-def>) unwrapped"
    out = unwrap_placeholder_brackets(src)
    assert "`[text](<526bf4e3-f775>)`" in out
    assert "[real](abc-def)" in out


def test_unwrap_idempotent():
    src = "A [t](<abc-def>) and ![a|800x600](<ref:xyz_1>)\n"
    once = unwrap_placeholder_brackets(src)
    assert unwrap_placeholder_brackets(once) == once


def test_unwrap_destination_is_mention_safe_and_extractable():
    """Binding, not literal: the unwrapped destination must pass SAFE_ID_RE and be
    returned by extract_doc_mentions / extract_ref_mentions — the two live surfaces
    the unwrap exists for — rather than asserting a string both sides spell out."""
    from db import SAFE_ID_RE
    from mentions import extract_doc_mentions, extract_ref_mentions

    doc_out = unwrap_placeholder_brackets("[text](<526bf4e3-f775-439a-a2b0>)")
    dest = doc_out[doc_out.index("(") + 1:doc_out.rindex(")")]
    assert SAFE_ID_RE.match(dest)
    assert extract_doc_mentions(doc_out) == ["526bf4e3-f775-439a-a2b0"]

    ref_out = unwrap_placeholder_brackets("![alt|800x600](<ref:abc_123>)")
    assert extract_ref_mentions(ref_out) == ["abc_123"]


def test_unwrap_not_wired_into_import_normalize():
    # normalize_markdown is the IMPORT choke point (.md upload / DOCX) — user
    # files, not agent-authored text: a bracketed destination there is the user's
    # own syntax and must survive.
    src = "[text](<526bf4e3-f775>)\n"
    assert "<526bf4e3-f775>" in normalize_markdown(src)
