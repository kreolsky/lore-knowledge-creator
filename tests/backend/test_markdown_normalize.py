"""Tests for markdown_normalize: dedent list-nested fences + reflow wrapped bullets.

The normalizer runs at reference-upload time so agent-written Markdown renders
cleanly in Lore's decoration-based CM6 editor (which mangles list-nested fenced
code and soft-wrapped bullet lines). See plan: normalize agent-written Markdown.
"""

import base64

from markdown_normalize import (
    normalize_list_spacing,
    normalize_markdown,
    strip_markdown_escapes,
    unwrap_placeholder_brackets,
)


def test_dedent_fence_out_of_list_item():
    src = (
        "- some bullet leading into a code example:\n"
        "\n"
        "  ```yaml\n"
        "  # До\n"
        "  m_echo_structure:\n"
        "    description: \"x\"\n"
        "  ```\n"
    )
    out = normalize_markdown(src)
    lines = out.split("\n")
    # Fence pulled to column 0.
    assert "```yaml" in lines
    assert "```" in lines
    # Body inner indentation preserved relative to the block (4 - 2 fence indent = 2).
    assert "# До" in out
    assert "  description: \"x\"" in out
    # Blank line guaranteed before the fence.
    fence_idx = lines.index("```yaml")
    assert lines[fence_idx - 1] == ""


def test_join_wrapped_bullet():
    src = (
        "- **В КАКОМ ВИДЕ** (единицы, формат) — если форма не задаётся\n"
        "  Markdown-шаблоном.\n"
    )
    out = normalize_markdown(src)
    assert out.count("\n- ") == 0  # single bullet
    assert "не задаётся Markdown-шаблоном." in out


def test_negative_sublist_not_folded():
    src = (
        "- parent item\n"
        "  - child item\n"
    )
    out = normalize_markdown(src)
    # Child remains its own list marker, not folded into parent.
    assert "child item" in out
    assert out.count("item") == 2
    # Two distinct marker lines survive.
    marker_lines = [ln for ln in out.split("\n") if ln.lstrip().startswith("- ")]
    assert len(marker_lines) == 2


def test_negative_fence_body_untouched():
    src = (
        "```\n"
        "- not a real bullet\n"
        "  indented line stays\n"
        "\n"
        "blank inside fence stays\n"
        "```\n"
    )
    out = normalize_markdown(src)
    assert "  indented line stays" in out
    assert "- not a real bullet" in out
    # The blank line inside the fence is preserved.
    assert "\n\nblank inside fence stays" in out


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


def test_top_level_fence_unchanged():
    src = (
        "Some text.\n"
        "\n"
        "```python\n"
        "x = 1\n"
        "```\n"
        "\n"
        "More text.\n"
    )
    assert normalize_markdown(src) == src


# ── Paragraph (prose) reflow ──────────────────────────────────────────────────

def test_reflow_prose_paragraph_single_newline():
    # Two hard-wrapped lines (single newline) → joined; the blank line (double
    # newline) between paragraphs is preserved.
    src = (
        "This is a fairly long first physical line of a paragraph the agent wrapped\n"
        "because it exceeded the column limit and continued onto a second source line.\n"
        "\n"
        "Second paragraph stays separate from the first one across the blank line gap.\n"
    )
    out = normalize_markdown(src)
    assert "the agent wrapped because it exceeded" in out
    assert "\n\nSecond paragraph" in out  # double newline preserved
    # First paragraph collapsed to one line.
    assert out.split("\n\n")[0].count("\n") == 0


def test_short_lines_not_joined():
    # Width gate: short standalone lines (below the wrap threshold) are NOT folded.
    src = "Short one.\nShort two.\n"
    out = normalize_markdown(src)
    assert "Short one.\nShort two." in out


# Link reference definitions are BLOCK constructs, not prose — the PDF converter
# emits embedded images as `[img-<hash>]: data:image/...;base64,<payload>` and
# reflow must never fold them. Observed live on scharf2012.pdf: two adjacent
# definitions merged into one undecodable payload (`Invalid base64 image data`).
_B64_A = "A" * 88  # 88 b64 chars -> 66 bytes
_B64_B = "B" * 88


def _decode_defs(md: str) -> dict[str, bytes]:
    """The consumer contract: files_service matches _REF_DEF_PATTERN on the
    NORMALIZED text and b64decodes each payload — corruption raises or decodes
    to wrong bytes."""
    import base64
    import re

    from files_service import _REF_DEF_PATTERN

    out = {}
    for m in _REF_DEF_PATTERN.finditer(md):
        b64 = re.sub(r"\s+", "", m.group(4))
        out[m.group(1)] = base64.b64decode(b64)
    return out


def test_reflow_never_folds_adjacent_image_definitions():
    src = (
        "# Title\n"
        "\n"
        f"[img-aaa]: data:image/jpeg;base64,{_B64_A}\n"
        f"[img-bbb]: data:image/png;base64,{_B64_B}\n"
        "\n"
        "Prose paragraph that follows the definitions and is long enough to look\n"
        "like wrapped source text for the reflow pass to want to fold it here.\n"
    )
    out = normalize_markdown(src)
    decoded = _decode_defs(out)
    assert decoded == {
        "img-aaa": base64.b64decode(_B64_A),
        "img-bbb": base64.b64decode(_B64_B),
    }


def test_giant_definition_does_not_skew_wrap_threshold():
    # A ~44k-char definition line is excluded from the width distribution, so the
    # sensed threshold comes from the PROSE and wrapped prose still folds.
    long_def = f"[img-aaa]: data:image/jpeg;base64,{_B64_A * 500}"
    src = (
        f"{long_def}\n"
        "\n"
        "This is a long hard-wrapped paragraph line one of several that should\n"
        "be folded back together into a single logical line by the reflow pass\n"
        "because each physical line is at or above the sensed wrap threshold.\n"
    )
    out = normalize_markdown(src)
    assert long_def in out.split("\n")
    para = [l for l in out.split("\n") if l.startswith("This is a long")]
    assert len(para) == 1
    assert "line one of several that should be folded" in para[0]


def test_reflow_does_not_fold_prose_into_definition():
    # No blank line: a definition directly above prose must stay its own line —
    # the prose below is not a "continuation" of the definition's payload.
    src = (
        f"[img-aaa]: data:image/png;base64,{_B64_A}\n"
        "A prose line directly after a definition with no blank line between.\n"
        "\n"
        "Second paragraph.\n"
    )
    out = normalize_markdown(src)
    assert f"[img-aaa]: data:image/png;base64,{_B64_A}" in out.split("\n")


def test_setext_heading_preserved():
    src = (
        "My Heading Title\n"
        "================\n"
        "\n"
        "Body paragraph text that is long enough to register as wrapped content here ok.\n"
    )
    out = normalize_markdown(src)
    assert "My Heading Title\n================" in out


def test_hard_break_preserved():
    # A line ending in two trailing spaces is an explicit <br>; the next line must
    # NOT be folded onto it even though the line is long enough to look wrapped.
    src = (
        "Line ending with a deliberate hard break carries two trailing spaces here  \n"
        "and this continuation must stay on its own line because of the hard break ok.\n"
    )
    out = normalize_markdown(src)
    assert "here  \nand this continuation" in out


def test_table_row_not_reflowed():
    src = "| a | b |\n| c | d |\n"
    assert normalize_markdown(src) == src


# ── Blockquote reflow (prose-with-prefix) ─────────────────────────────────────

def test_reflow_blockquote():
    src = (
        "> This is a hard-wrapped blockquote first line that runs near the column edge\n"
        "> and continues on a second quoted source line that should fold into one line.\n"
    )
    out = normalize_markdown(src)
    # Folded into a single quote line.
    assert out.strip().count("\n") == 0
    assert out.startswith("> This is")
    assert "fold into one line." in out


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


def test_reflow_idempotent_with_prose_and_quote():
    src = (
        "A wrapped paragraph whose first physical line is comfortably past the wrap\n"
        "boundary and therefore folds with the line that immediately follows it here.\n"
        "\n"
        "> A wrapped quote whose first physical line is also comfortably past the wrap\n"
        "> boundary and folds with the quoted line that immediately follows it as well.\n"
    )
    once = normalize_markdown(src)
    assert once == normalize_markdown(once)


# ── List/task spacing normalization ─────────────────────────────────────────
# LLMs emit task-list / bullet markdown with an irregular run of spaces after the
# marker — e.g. `*   [ ]` (three spaces) — which breaks Lore's CM6 flicker guard
# and renders poorly. normalize_list_spacing collapses the after-marker (and
# after-task-marker) run of spaces to a single space, preserving leading indent.


def test_normalize_list_spacing_collapses_bullet_spaces():
    assert normalize_list_spacing("-   item") == "- item"
    assert normalize_list_spacing("*    item") == "* item"
    assert normalize_list_spacing("+   item") == "+ item"


def test_normalize_list_spacing_collapses_task_spaces():
    assert normalize_list_spacing("*   [ ] foo") == "* [ ] foo"
    assert normalize_list_spacing("-    [x] bar") == "- [x] bar"
    assert normalize_list_spacing("*   [X] baz") == "* [X] baz"


def test_normalize_list_spacing_collapses_spaces_after_task_marker():
    # Extra spaces between `]` and content also collapse.
    assert normalize_list_spacing("* [ ]    foo") == "* [ ] foo"


def test_normalize_list_spacing_preserves_indentation():
    # Leading indentation (nesting depth) is preserved verbatim.
    assert normalize_list_spacing("    -   nested") == "    - nested"
    assert normalize_list_spacing("  *   [ ] sub") == "  * [ ] sub"


def test_normalize_list_spacing_ordered_list():
    assert normalize_list_spacing("1.   first") == "1. first"
    assert normalize_list_spacing("12.   wide") == "12. wide"


def test_normalize_list_spacing_fenced_code_untouched():
    src = (
        "```\n"
        "*   not a real bullet\n"
        "-   [ ] stays\n"
        "```\n"
    )
    assert normalize_list_spacing(src) == src


def test_normalize_list_spacing_indented_fence_closes():
    # An indented closing fence (≤3 spaces is valid CommonMark) must be recognized
    # on the standalone path — otherwise in_fence latches true and the post-fence
    # task line never gets its spacing collapsed. Regression for the _FENCE_ONLY
    # column-0 asymmetry that affected export/agent/extractor (no pre-dedent).
    src = (
        "  ```\n"
        "*   not a real bullet\n"
        "  ```\n"
        "*   [ ] task after fence\n"
    )
    out = normalize_list_spacing(src)
    assert "*   [ ] task after fence" not in out
    assert "* [ ] task after fence" in out
    # Inside-fence content stays untouched.
    assert "*   not a real bullet" in out


def test_normalize_list_spacing_no_marker_unchanged():
    assert normalize_list_spacing("plain prose line") == "plain prose line"
    assert normalize_list_spacing("# Heading") == "# Heading"
    assert normalize_list_spacing("") == ""


def test_normalize_list_spacing_idempotent():
    src = "*   [ ]   foo\n-   bar\n"
    once = normalize_list_spacing(src)
    assert once == "* [ ] foo\n- bar\n"
    assert normalize_list_spacing(once) == once


def test_normalize_list_spacing_multi_line_doc():
    src = (
        "## Checklist\n"
        "\n"
        "*   [ ] First item\n"
        "*   [x] Second item\n"
        "-   [ ] Third item\n"
        "\n"
        "Some prose.\n"
    )
    out = normalize_list_spacing(src)
    assert "*   [ ]" not in out
    assert "* [ ] First item" in out
    assert "* [x] Second item" in out
    assert "- [ ] Third item" in out


def test_normalize_markdown_collapses_list_spacing():
    # The import path (uploaded .md, DOCX→md) runs normalize_markdown, which must
    # also collapse irregular post-marker spacing — not just reflow/dedent.
    src = "## Checklist\n\n*   [ ] First\n-   plain item\n"
    out = normalize_markdown(src)
    assert "*   [ ]" not in out
    assert "* [ ] First" in out
    assert "- plain item" in out


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


# ── Backslash-escape stripping (Pandoc/Word export noise) ────────────────────
# Imported .md and DOCX→md carry defensive escapes (\- \[ \] \\ \_ \. \# \= \< \>
# \~) that render as stray backslashes in CM6. strip_markdown_escapes replaces
# every `\X` (X = ASCII punctuation) with `X`; a backslash before a non-punct
# char is an invalid CommonMark escape and stays literal. Fence-aware: code
# block bodies are copied through unchanged. See plan: import escape-stripping.


def test_strip_em_dash_escape_in_bold():
    # `\-` inside bold prose → `-`.
    assert strip_markdown_escapes("**… Ti \\- 50.9 at% Ni**") == "**… Ti - 50.9 at% Ni**"


def test_strip_backslash_runs_in_image_path():
    # `\\` (literal backslash) and `\_` (literal underscore) collapse correctly.
    src = r"![D:\\GIRS\\SG\\Статьи и конф\_2025\\R\\fig1a\_ML.tif][image1]"
    assert strip_markdown_escapes(src) == (
        r"![D:\GIRS\SG\Статьи и конф_2025\R\fig1a_ML.tif][image1]"
    )


def test_strip_escaped_dot_in_caption():
    assert strip_markdown_escapes("Fig. 4\\.") == "Fig. 4."
    assert strip_markdown_escapes("Fig. 2\\.") == "Fig. 2."


def test_strip_exposes_ordered_list_marker():
    # `4\.` → `4.` ; the tab after the marker is preserved (not a space run, so
    # normalize_list_spacing leaves it — the marker is now exposed regardless).
    assert strip_markdown_escapes("4\\.\tConclusions") == "4.\tConclusions"


def test_strip_misc_punct_escapes():
    assert strip_markdown_escapes(r"\=") == "="
    assert strip_markdown_escapes(r"\<") == "<"
    assert strip_markdown_escapes(r"\>") == ">"
    assert strip_markdown_escapes(r"\~") == "~"
    assert strip_markdown_escapes(r"\#") == "#"


def test_strip_preserves_backslash_before_non_punct():
    # Invalid CommonMark escapes (backslash before non-punctuation) stay literal.
    assert strip_markdown_escapes(r"C:\Temp\5folder") == r"C:\Temp\5folder"
    assert strip_markdown_escapes("\\д") == "\\д"  # backslash + Cyrillic letter


def test_strip_preserves_inline_code():
    # Backslashes inside inline-code spans are literal code, never stripped —
    # only the gaps around the spans get the escape strip.
    assert strip_markdown_escapes(r"use `\.dot` regex") == r"use `\.dot` regex"
    assert strip_markdown_escapes(r"`C:\Temp\5` and \- dash") == r"`C:\Temp\5` and - dash"


def test_strip_fence_body_untouched():
    # Backslashes inside a fenced code block are literal code, never stripped.
    src = "```\n" + r"\- \[ \] \\ \_ \. \#" + "\n```\n"
    assert strip_markdown_escapes(src) == src


def test_strip_outside_fence_only():
    # Escapes before/after a fence are stripped; the fence body is copied verbatim.
    fence_body = r"\- \[ \] \\ \_ \. \#"
    src = "Fig. 4\\.\n```\n" + fence_body + "\n```\nsee Fig. 2\\.\n"
    out = strip_markdown_escapes(src)
    assert out.startswith("Fig. 4.\n")
    assert out.endswith("see Fig. 2.\n")
    assert fence_body in out  # fence body intact


def test_strip_idempotent():
    # Running twice == once on realistic import content; no `\punct` survives
    # outside fenced code.
    src = (
        r"**Ti \- 50.9 at% Ni**"
        "\n"
        r"![D:\\path\\fig\_1.tif][img]"
        "\n"
        r"see \[ref\] and Fig. 4\."
        "\n"
    )
    once = strip_markdown_escapes(src)
    assert strip_markdown_escapes(once) == once
    for seq in (r"\-", r"\[", r"\]", r"\.", r"\_", r"\#"):
        assert seq not in once


def test_normalize_markdown_strips_escapes():
    # The import choke point (normalize_markdown) must run the escape strip.
    src = r"**Ti \- 50.9** and Fig. 4\." + "\n"
    out = normalize_markdown(src)
    assert "\\-" not in out
    assert "Ti - 50.9" in out
    assert "Fig. 4." in out


def test_normalize_markdown_strip_before_list_spacing():
    # Stripping runs BEFORE normalize_list_spacing so a newly-exposed `4.` marker
    # gets recognized and its spacing normalized.
    src = "4\\.   Conclusions\n"
    out = normalize_markdown(src)
    assert out == "4. Conclusions\n"


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
