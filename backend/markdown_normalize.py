r"""Normalize externally-authored Markdown for Lore's decoration-based CM6 editor.

# SYSTEM: markdown content normalize — the choke points that keep Markdown clean as it
# enters OR leaves a Lore document, regardless of source. Public entry points:
#   normalize_markdown          — full clean for IMPORT: .md upload (routes/files.py) and
#                                 DOCX→md conversion (jobs/tasks.py). Runs all import passes
#                                 (dedent, reflow, tight-list, escape-strip, list-spacing).
#   normalize_list_spacing      — the spacing pass alone, reused where reflow/dedent are
#                                 unwanted (content must not be reformatted wholesale):
#                                   · agent apply   (agent/tool_api_surface.py)
#                                   · extractor      (pipeline/extractor/runner.py)
#                                   · export         (routes/documents.py — output side:
#                                     cleans even manually-typed collab content on the way out).
#   unwrap_placeholder_brackets — strips `(<id>)` link-destination angle brackets the
#                                 agent copies from prompt placeholders. Agent write
#                                 sites ONLY (the three normalize_list_spacing agent
#                                 entries below), never import/extractor/export.
# Write-side entries keep the STORED document clean; the export entry guarantees a clean
# document even when it was authored by a path that bypasses the write-side normalizers
# (manual collab typing). Defense-in-depth with the tolerant CM6 + chat renderers.

Markdown from these sources (LLM agents and imported files alike) carries cosmetic
shapes that are valid CommonMark but mangled by Lore's live CM6 renderer
(frontend/src/editor/):

1. Fenced code blocks indented to sit *inside* a list item.
2. Prose, list items and blockquotes **hard-wrapped at a fixed column** (~100) — a
   single newline mid-paragraph for diff-friendly source.
3. Irregular runs of spaces after a list/task marker, for example `*   [ ]` (LLM-emitted).

# ARCH: We fix this at write time, NOT in the fragile renderer (codemirror.md). The
# transforms are pure, line-based and fence-aware. Five passes (in pipeline order):
#   _dedent_fences            — pull list-nested fences to column 0.
#   _reflow                   — fold soft-wrapped lines back into one logical line.
#   _collapse_list_item_blanks — drop blank lines between consecutive list items
#                                (loose→tight); import-only.
#   strip_markdown_escapes    — remove Pandoc/Word backslash escape noise (\X→X);
#                               import-only; runs before list-spacing so a marker
#                               unescaped from `4\.` gets normalized.
#   normalize_list_spacing    — collapse post-marker space runs to a single space.
#   unwrap_placeholder_brackets — turn `(<id>)` link destinations into `(id)`;
#                               agent write sites only, never import/export.

# WHY reflow is safe: in CommonMark a *single* newline inside a paragraph/quote is a
# soft break that renders as a single space anyway — joining it changes nothing about
# the rendered output, it only removes source line breaks the CM6 editor shows
# literally. A *double* newline (blank line) is a real paragraph boundary and is never
# crossed. Hence: single newline → join, blank line → keep. (User decision, 2026-06-19.)
"""

import re

_FENCE_OPEN = re.compile(r"^(\s*)(`{3,}|~{3,})")
_FENCE_ONLY = re.compile(r"^(`{3,}|~{3,})\s*$")
_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+\.)\s+\S")
_QUOTE = re.compile(r"^(\s*)>\s?(.*)$")
_UNDERLINE_OR_RULE = re.compile(r"(=+|-+|_{3,}|\*{3,})")
_INLINE_CODE = re.compile(r"`[^`]*`")

# A link reference definition line (`[label]: destination`) — the form the PDF
# converter emits for embedded images (`[img-<hash>]: data:image/png;base64,…`).
# WHY excluded from prose/continuation: reflow folds soft-wrapped PROSE, but a
# definition is a block construct whose destination is ONE logical token (a
# ~90k-char base64 payload). Folding two adjacent definitions (or a definition
# into a neighbouring paragraph) concatenates the payloads and corrupts the
# base64 — observed live on scharf2012.pdf: two adjacent 560x428 jpeg
# definitions merged into one undecodable line (`Invalid base64 image data`),
# and the definition lengths also skewed _detect_wrap_threshold so the whole
# document over-folded. Excluding them fixes both.
_REF_DEF_LINE = re.compile(r"^\[[^\]]+\]:")

# A backslash escape: `\` immediately followed by one ASCII punctuation char. These
# are the only chars a backslash can legitimately escape in CommonMark; a backslash
# before any other char is an invalid escape and stays literal. Captures the punct so
# the replacement is a single left-to-right, non-overlapping pass — `\\`→`\` collapses
# a backslash run correctly without re-scanning the emitted char.
# SYSTEM: markdown escape strip — removes Pandoc/Word export backslash noise.
_STRIP_ESCAPE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~])")

# WHY: a source line shorter than the wrap threshold is treated as a deliberate
# line end and is NOT folded into the next line. Why: the agent hard-wraps near a fixed
# column, so a *full-width* line signals "this is a wrap continuation"; a short line is
# an intentional paragraph/last line. The threshold is detected per-document (the wrap
# width sensed from the line-length distribution) and falls back to this default when
# the document is too small to sense a width. Without this gate, two deliberately
# separate short lines with no blank between them would be wrongly merged.
_DEFAULT_WRAP_THRESHOLD = 64


def _strip_lead(line: str, n: int) -> str:
    """Strip up to ``n`` leading space characters (preserves deeper inner indent)."""
    j = 0
    while j < n and j < len(line) and line[j] == " ":
        j += 1
    return line[j:]


def _is_underline_or_rule(s: str) -> bool:
    """A setext underline (`===`/`---`) or a thematic break (`---`/`***`/`___`)."""
    return bool(_UNDERLINE_OR_RULE.fullmatch(s.strip()))


def _looks_like_table(s: str) -> bool:
    """A `|` outside an inline-code span — i.e. a real table cell separator.

    WHY: prose/quotes routinely carry literal pipes inside inline code
    (`` `kind: a | b` ``); those must not be mistaken for a table row and block reflow.
    """
    return "|" in _INLINE_CODE.sub("", s)


def _is_prose(line: str) -> bool:
    """A plain prose line: not blank and not the start of any other block construct.

    Excludes headings, blockquotes, fences, list items, tables, and setext/thematic
    rules — so reflow only ever folds genuine prose, and a setext heading's underline
    is left attached to its title (the run ends at the non-prose underline line).
    """
    s = line.strip()
    if not s or s[0] in "#>":
        return False
    if s.startswith(("```", "~~~")) or _LIST_ITEM.match(line) or _looks_like_table(s):
        return False
    if _REF_DEF_LINE.match(s):
        return False
    return not _is_underline_or_rule(s)


def _has_hard_break(line: str) -> bool:
    """An explicit Markdown line break: two trailing spaces or a trailing backslash.

    INVARIANT: a hard-broken line is never folded into its successor.
    Why: the trailing `  ` / `\\` is an intentional <br>; folding would delete it.
    """
    return line.endswith("  ") or line.rstrip().endswith("\\")


def _is_continuation(line: str) -> bool:
    """A soft-wrapped *list item* continuation: indented, non-blank, and not the start
    of any other block construct. List items reflow on indentation (a continuation is
    indented under the marker), independent of the prose wrap-width gate."""
    if line.strip() == "" or line[0] not in (" ", "\t"):
        return False
    s = line.strip()
    if _LIST_ITEM.match(s):
        return False
    if _REF_DEF_LINE.match(s):
        return False
    return not (s.startswith(("```", "~~~", "#", ">")) or _looks_like_table(s))


def _dedent_fences(lines: list[str]) -> list[str]:
    """Pull indented (list-nested) fenced code blocks to column 0, guaranteeing a
    blank line before and after so they parse as standalone top-level blocks.

    INVARIANT: content inside a fence is only de-indented by the block's own common
    indent — inner relative indentation is preserved verbatim.
    Why: the fence body may be indentation-significant (YAML, Python).
    """
    out: list[str] = []
    in_fence = False
    fence_indent = 0
    fence_char = ""
    pending_blank = False
    for line in lines:
        if pending_blank:
            if line.strip() != "":
                out.append("")
            pending_blank = False
        if not in_fence:
            m = _FENCE_OPEN.match(line)
            if m:
                fence_indent = len(m.group(1))
                fence_char = m.group(2)[0]
                if fence_indent > 0:
                    if out and out[-1].strip() != "":
                        out.append("")
                    out.append(_strip_lead(line, fence_indent))
                else:
                    out.append(line)
                in_fence = True
            else:
                out.append(line)
        else:
            stripped = _strip_lead(line, fence_indent)
            out.append(stripped)
            if _FENCE_ONLY.match(stripped) and stripped[0] == fence_char:
                in_fence = False
                if fence_indent > 0:
                    pending_blank = True
    return out


def _detect_wrap_threshold(lines: list[str]) -> int:
    """Sense the hard-wrap width from the prose/quote line-length distribution.

    Returns ``int(0.85-percentile-width * 0.8)`` — full lines (incl. the `> ` prefix)
    are measured so the threshold is comparable across prose and quotes. Falls back to
    ``_DEFAULT_WRAP_THRESHOLD`` when there is too little signal (< 3 lines, or the
    sensed width is implausibly small). Fence bodies are excluded.
    """
    measured: list[int] = []
    in_fence = False
    fence_char = ""
    for line in lines:
        if not in_fence:
            m = _FENCE_OPEN.match(line)
            if m:
                in_fence = True
                fence_char = m.group(2)[0]
                continue
            if _is_prose(line) or _QUOTE.match(line):
                measured.append(len(line))
        elif _FENCE_ONLY.match(line) and line.lstrip()[:1] == fence_char:
            in_fence = False
    if len(measured) < 3:
        return _DEFAULT_WRAP_THRESHOLD
    measured.sort()
    width = measured[int(len(measured) * 0.85)]
    if width < 50:
        return _DEFAULT_WRAP_THRESHOLD
    return int(width * 0.8)


def _reflow_block(block: list[str], threshold: int, offset: int) -> list[str]:
    """Fold soft-wrapped prose lines within one contiguous block into single lines.

    Shared by prose paragraphs (``offset=0``) and blockquote inner content
    (``offset=len('> ')`` so the gate compares against the full visible line width).
    A line is folded onto its predecessor only when the predecessor reached the wrap
    threshold and is not hard-broken; non-prose lines (and bare blank quote lines) are
    emitted verbatim and act as separators.
    """
    out: list[str] = []
    i = 0
    n = len(block)
    while i < n:
        line = block[i]
        if not _is_prose(line):
            out.append(line)
            i += 1
            continue
        buf = line
        prev = line
        i += 1
        while i < n and _is_prose(block[i]):
            if offset + len(prev) >= threshold and not _has_hard_break(prev):
                buf = buf.rstrip() + " " + block[i].strip()
            else:
                out.append(buf)
                buf = block[i]
            prev = block[i]
            i += 1
        out.append(buf)
    return out


def _reflow(lines: list[str]) -> list[str]:
    """Single fence-aware pass folding soft-wrapped list items, prose and blockquotes.

    Fence bodies are copied through untouched. List items fold on indentation; prose
    and blockquotes fold on the detected wrap-width gate (blockquotes as prose with a
    uniform `> ` prefix — same heuristics, user decision 2026-06-19).
    """
    threshold = _detect_wrap_threshold(lines)
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        fm = _FENCE_OPEN.match(line)
        if fm:
            out.append(line)
            fence_char = fm.group(2)[0]
            i += 1
            while i < n:
                out.append(lines[i])
                closed = _FENCE_ONLY.match(lines[i]) and lines[i].lstrip()[:1] == fence_char
                i += 1
                if closed:
                    break
            continue
        if _LIST_ITEM.match(line):
            buf = line
            i += 1
            while i < n and _is_continuation(lines[i]):
                buf = buf.rstrip() + " " + lines[i].strip()
                i += 1
            out.append(buf)
            continue
        qm = _QUOTE.match(line)
        if qm:
            indent = qm.group(1)
            inner: list[str] = []
            while i < n:
                m = _QUOTE.match(lines[i])
                if not m:
                    break
                inner.append(m.group(2))
                i += 1
            for folded in _reflow_block(inner, threshold, len(indent) + 2):
                out.append((indent + "> " + folded).rstrip())
            continue
        if _is_prose(line):
            run: list[str] = []
            while i < n and _is_prose(lines[i]):
                run.append(lines[i])
                i += 1
            out.extend(_reflow_block(run, threshold, 0))
            continue
        out.append(line)
        i += 1
    return out


def _collapse_list_item_blanks(lines: list[str]) -> list[str]:
    """Drop blank-line runs sandwiched between two consecutive list-item lines.

    DOCX→md (Pandoc) and some hand-authored Markdown emit *loose* lists — a blank line
    between every item. On import we want *tight* lists (one item per line). Runs after
    `_reflow`, so multi-line items are already folded to a single line; the "previous
    line is a list item" check is therefore reliable. Fence-aware: blank lines inside a
    fenced code block are copied through untouched.

    INVARIANT: a blank-line run is removed only when BOTH bordering non-blank lines are
    list items. Why: a loose list imported from docx should render tight, but a blank
    line ending a list (list → prose, list → heading, or a list-item's own continuation
    paragraph) is a real boundary and must survive.
    """
    out: list[str] = []
    in_fence = False
    fence_char = ""
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not in_fence:
            m = _FENCE_OPEN.match(line)
            if m:
                in_fence = True
                fence_char = m.group(2)[0]
                out.append(line)
                i += 1
                continue
        else:
            out.append(line)
            stripped = line.lstrip()
            if _FENCE_ONLY.match(stripped) and stripped[:1] == fence_char:
                in_fence = False
            i += 1
            continue
        if line.strip() == "":
            j = i
            while j < n and lines[j].strip() == "":
                j += 1
            prev_is_item = bool(out) and bool(_LIST_ITEM.match(out[-1]))
            next_is_item = j < n and bool(_LIST_ITEM.match(lines[j]))
            if prev_is_item and next_is_item:
                i = j  # drop the blank run between the two items
                continue
            out.extend(lines[i:j])
            i = j
            continue
        out.append(line)
        i += 1
    return out


def normalize_markdown(content: str) -> str:
    """Dedent list-nested fenced code to column 0, then reflow soft-wrapped lines.

    Folds single-newline wraps in prose, list items and blockquotes; preserves blank
    lines (paragraph boundaries), fenced code, hard breaks and setext/thematic rules.
    Collapses loose lists (blank line between consecutive items) to tight on import,
    and strips defensive backslash escapes (Pandoc/Word export noise). Idempotent.
    Preserves the trailing-newline shape of the input.
    """
    lines = content.split("\n")
    lines = _dedent_fences(lines)
    lines = _reflow(lines)
    lines = _collapse_list_item_blanks(lines)
    # Strip escapes BEFORE list-spacing so a newly-exposed marker (e.g. `4.` from
    # `4\.`) gets recognized and spacing-normalized by the next pass.
    content = strip_markdown_escapes("\n".join(lines))
    # Collapse irregular post-marker spacing (e.g. `*   [ ]`) last — imported docs
    # (uploaded .md, DOCX→md) get the same clean spacing as agent-applied content.
    return normalize_list_spacing(content)


def strip_markdown_escapes(content: str) -> str:
    r"""Strip defensive backslash escapes (Pandoc/Word export noise) from Markdown.

    Imported ``.md`` and DOCX→md (Pandoc) documents carry escapes like ``\-``, ``\[``,
    ``\]``, ``\\``, ``\_``, ``\.``, ``\#``, ``\=``, ``\<``, ``\>``, ``\~`` that render
    as visible stray backslashes in Lore's CM6 editor. This replaces every ``\X``
    (``X`` = ASCII punctuation) with the single char ``X``. A backslash before a
    non-punctuation char is an invalid CommonMark escape and stays literal — e.g.
    ``C:\Temp\5folder`` and ``\д`` are unchanged.

    # WHY a single left-to-right pass: Python ``re`` matches non-overlapping and
    # consumes BOTH chars of ``\X``, so the emitted punctuation is never re-scanned
    # and a backslash run collapses correctly (``\\`` → ``\``).
    # INVARIANT: code backslashes are literal.  Why: outside code a backslash is an escape to collapse; inside fenced/inline code it's literal text, so those spans are copied unchanged or the code's content would be corrupted. Two contexts are copied through
    # UNCHANGED: (1) lines inside ``` fenced code blocks; (2) inline-code spans
    # (`` `…` ``) within a prose line — stripping only the gaps between spans
    # (mirrors the ``_INLINE_CODE`` handling in ``_looks_like_table``). The fence
    # tracker reuses ``normalize_list_spacing``'s pattern, including the lstripped
    # closer recognition so an indented closing fence (≤3 spaces, valid CommonMark)
    # is handled on the standalone call paths (no pre-dedent there). Idempotent on
    # realistic import content: the output contains no ``\punct`` sequence outside
    # fenced code and inline-code spans.
    """
    lines = content.split("\n")
    out: list[str] = []
    in_fence = False
    fence_char = ""
    for line in lines:
        if not in_fence:
            m = _FENCE_OPEN.match(line)
            if m:
                in_fence = True
                fence_char = m.group(2)[0]
                out.append(line)
                continue
        else:
            out.append(line)
            # Match the closer against the lstripped line so an indented closing
            # fence (≤3 spaces is valid CommonMark) is recognized — _FENCE_ONLY is
            # anchored at column 0 and would otherwise miss it, latching in_fence
            # true and skipping stripping for the rest of the document on the
            # standalone call paths that never pre-dedent (mirrors
            # normalize_list_spacing).
            stripped = line.lstrip()
            if _FENCE_ONLY.match(stripped) and stripped[:1] == fence_char:
                in_fence = False
            continue
        out.append(_strip_escapes_line(line))
    return "\n".join(out)


def _strip_escapes_line(line: str) -> str:
    r"""Strip ``\punct`` escapes from one line, preserving inline-code spans verbatim.

    Splits the line on `` `…` `` spans (``_INLINE_CODE``) and applies the escape
    strip only to the gaps between them — backslashes inside inline code are literal
    CommonMark and must not be touched (e.g. `` `\.dot` `` stays `` `\.dot` ``).
    """
    chunks: list[str] = []
    last = 0
    for m in _INLINE_CODE.finditer(line):
        chunks.append(_STRIP_ESCAPE.sub(r"\1", line[last:m.start()]))
        chunks.append(m.group(0))  # inline-code span: copy verbatim
        last = m.end()
    chunks.append(_STRIP_ESCAPE.sub(r"\1", line[last:]))
    return "".join(chunks)


# Collapses the run of spaces immediately AFTER a list marker (-, *, +, N.) and
# after a task marker ([ ]/[x]/[X]) down to a single space. Leading indentation is
# left untouched (nesting depth must be preserved).
_MARKER_SPACES = re.compile(r"^(\s*)([-*+]|\d+\.)( +)")
_TASK_SPACES = re.compile(r"^(\s*(?:[-*+]|\d+\.) \[[ xX]\])( +)")


def normalize_list_spacing(content: str) -> str:
    """Collapse irregular runs of spaces after list/task markers to a single space.

    LLMs frequently emit task-list / bullet Markdown with an irregular number of
    spaces — e.g. ``*   [ ]`` (three spaces after the marker) instead of
    ``* [ ]``. Lezer still parses such a line but Lore's CM6 flicker guard and the
    chat renderer expect a single space, so multi-space forms render poorly.

    # ARCH: applied at every write choke point — agent apply (edit + create
    # proposals) AND import (normalize_markdown, i.e. .md upload + DOCX→md) — so the
    # STORED document is always clean, as defense-in-depth alongside the tolerant
    # renderers. Fence-aware: lines inside ``` blocks are copied through untouched.

    Leading indentation (nesting depth) is preserved verbatim. Idempotent.
    """
    lines = content.split("\n")
    out: list[str] = []
    in_fence = False
    fence_char = ""
    for line in lines:
        if not in_fence:
            m = _FENCE_OPEN.match(line)
            if m:
                in_fence = True
                fence_char = m.group(2)[0]
                out.append(line)
                continue
        else:
            out.append(line)
            # Match the closer against the lstripped line so an indented closing
            # fence (≤3 spaces is valid CommonMark) is recognized. _FENCE_ONLY is
            # anchored at column 0 and would otherwise miss it, latching in_fence
            # true and skipping normalization for the rest of the document on the
            # standalone call paths (export / agent / extractor) that never
            # pre-dedent — unlike normalize_markdown, which runs _dedent_fences first.
            stripped = line.lstrip()
            if _FENCE_ONLY.match(stripped) and stripped[:1] == fence_char:
                in_fence = False
            continue
        # Collapse marker→content spaces, then task-marker→content spaces.
        line = _MARKER_SPACES.sub(r"\1\2 ", line)
        line = _TASK_SPACES.sub(r"\1 ", line)
        out.append(line)
    return "\n".join(out)


# A link destination the agent copied out of a prompt placeholder verbatim:
# `(<id>)`, `(<ref:id>)`, `(<table:id>)`. The body charset mirrors SAFE_ID_RE
# (db._patch) — the binding test in test_markdown_normalize.py pins the two
# together. Everything else in brackets (notably `<https://…>` autolinks, where
# the brackets are legal and MEANT) fails the charset and never matches.
_PLACEHOLDER_DEST_RE = re.compile(r"\(<((?:ref:|table:)?[a-zA-Z0-9_\-]+)>\)")


def _unwrap_placeholders_line(line: str) -> str:
    """Unwrap placeholder destinations on one line, preserving inline-code spans.

    Splits the line on `` `…` `` spans (``_INLINE_CODE``) and unwraps only the gaps
    between them — a span like `` `[text](<id>)` `` is prose ABOUT the syntax and
    must survive verbatim (mirrors ``_strip_escapes_line``).
    """
    from db import SAFE_ID_RE

    def _repl(m: re.Match) -> str:
        body = m.group(1)
        for prefix in ("ref:", "table:"):
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
        return f"({m.group(1)})" if SAFE_ID_RE.match(body) else m.group(0)

    chunks: list[str] = []
    last = 0
    for m in _INLINE_CODE.finditer(line):
        chunks.append(_PLACEHOLDER_DEST_RE.sub(_repl, line[last:m.start()]))
        chunks.append(m.group(0))  # inline-code span: copy verbatim
        last = m.end()
    chunks.append(_PLACEHOLDER_DEST_RE.sub(_repl, line[last:]))
    return "".join(chunks)


def unwrap_placeholder_brackets(content: str) -> str:
    r"""Strip the angle brackets a copied prompt placeholder leaves on a link
    destination: ``[text](<id>)`` becomes ``[text](id)``, same for the
    ``ref:``/``table:`` image and table forms.

    The agent's instructions spell link slots as ``[text](<id>)``; ``<...>`` is a
    legal CommonMark destination, so a weak model cannot tell the slot from the
    syntax and writes ``[text](<uuid>)`` verbatim. ``extract_doc_mentions``
    (mentions.py) runs the destination through ``SAFE_ID_RE`` — the brackets fail
    it, the text is stored, and the link renders broken. This pass unwraps the
    destination at write time.

    # WHY: restricted to a bare id (SAFE_ID_RE) or a ``ref:``/``table:`` form, so
    # an autolink destination like ``[text](<https://…>)`` — where the brackets are
    # legal and meant — is never touched.
    # WHY: called at the AGENT write sites only (the three normalize_list_spacing
    # agent entries), never import/extractor/export — this is an agent-authoring
    # defect; export is the output side.
    # Fence-aware (lines inside ``` blocks are copied through untouched) and
    # inline-code-aware (prose ABOUT the syntax inside backticks survives).
    Idempotent.
    """
    lines = content.split("\n")
    out: list[str] = []
    in_fence = False
    fence_char = ""
    for line in lines:
        if not in_fence:
            m = _FENCE_OPEN.match(line)
            if m:
                in_fence = True
                fence_char = m.group(2)[0]
                out.append(line)
                continue
        else:
            out.append(line)
            stripped = line.lstrip()
            if _FENCE_ONLY.match(stripped) and stripped[:1] == fence_char:
                in_fence = False
            continue
        out.append(_unwrap_placeholders_line(line))
    return "\n".join(out)
