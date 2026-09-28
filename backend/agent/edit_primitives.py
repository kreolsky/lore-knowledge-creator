"""Pure edit-resolution primitives shared by the apply paths and selection-conflict.

# Edit-range resolution + splice (see SYSTEM: chat-agent-mode, agent/__init__.py).

This is a LEAF in the agent-package import DAG. `selection_conflict`,
`tool_api_surface`, and `proposals` import these primitives; nothing here
imports a sibling agent module. `resolve_edit_range` is public because it is
consumed across the package boundary (`selection_conflict`).
"""
import re
import unicodedata

# Zero-width chars dropped by the fold; NBSP variants folded to a plain space.
_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\ufeff")
_NBSP = frozenset("\u00a0\u202f")


def deescape_control_chars(s: str) -> str:
    """De-escape the literal `\\n`/`\\t`/`\\r` two-char sequences (JSON.stringify artifact).

    Single definition shared by the fold and the callers' symmetric new_string
    de-escape — the two must apply the IDENTICAL transformation.
    """
    return s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")


def _fold_content(content: str) -> tuple[str, list[int]]:
    """Fold content to a normalized projection WITH an offset map to original code points.

    Returns (folded, starts) where starts[k] is the original code-point index at
    which folded[k] begins; the slot after the last folded char maps implicitly to
    len(content). Transforms (see plan normalized-fold-edit-matching §3):
      - `\r\n` / lone `\r` → `\n`
      - strip `[ \t]+` before each `\n` and at end-of-string
      - NBSP (U+00A0) / narrow NBSP (U+202F) → space
      - drop zero-width chars (U+200B/200C/200D/FEFF)
      - per-char NFC (cross-code-point combining is deferred; see the module tests)

    NOT applied here: literal-escape de-escape — content already holds real chars;
    de-escaping it would corrupt offsets for docs that legitimately contain `\\n`.
    """
    folded: list[str] = []
    starts: list[int] = []
    pending: list[tuple[str, int]] = []  # space/tab run awaiting flush-or-drop
    n = len(content)
    i = 0
    while i < n:
        ch = content[i]
        if ch in " \t":
            pending.append((ch, i))
            i += 1
            continue
        if ch in "\r\n":
            pending.clear()  # trailing spaces/tabs before a newline are dropped
            folded.append("\n")
            starts.append(i)
            # \r\n collapses to a single \n mapped to the \r's index.
            i += 2 if ch == "\r" and i + 1 < n and content[i + 1] == "\n" else 1
            continue
        # A non-space, non-newline char: the pending space run is interior → keep it.
        for c, idx in pending:
            folded.append(c)
            starts.append(idx)
        pending.clear()
        if ch in _ZERO_WIDTH:
            i += 1
            continue
        if ch in _NBSP:
            folded.append(" ")
            starts.append(i)
            i += 1
            continue
        norm = unicodedata.normalize("NFC", ch)
        for c in norm:  # per-char NFC is 1:1 for the vast majority; map any expansion to i
            folded.append(c)
            starts.append(i)
        i += 1
    # Trailing pending spaces/tabs at end-of-string are dropped.
    return "".join(folded), starts


def _fold_old_string(old_string: str) -> str:
    """Fold old_string with the SAME transforms as the content side, plus literal
    de-escape first (old_string may carry JSON.stringify escapes; content never does)."""
    folded, _ = _fold_content(deescape_control_chars(old_string))
    return folded


def fold_new_string(s: str) -> str:
    """The fold minus the offset-irrelevant parts, for new_string (no offset map).

    Applies de-escape, `\r\n`→`\n`, NBSP→space, zero-width drop, NFC — but NOT
    trailing-space stripping: the replacement's spacing is the model's intent, and
    normalize_list_spacing runs afterward at the call sites.
    """
    s = deescape_control_chars(s).replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\u00a0", " ").replace("\u202f", " ")
    for z in _ZERO_WIDTH:
        s = s.replace(z, "")
    return unicodedata.normalize("NFC", s)


# A line that opens a Markdown BLOCK: ATX heading, list/task item, blockquote,
# fence, table row. Deliberately strict — the space after `#`/`-`/`1.` is what
# separates a real construct from prose (`#хэштег`, `1.пункт`, `-минус`), so an
# inline replacement is never mistaken for a block.
_BLOCK_LINE = re.compile(r"^(?:#{1,6} |[-*+] |\d+[.)] |> |```|~~~|\|)")


def _is_block_line(line: str) -> bool:
    return bool(_BLOCK_LINE.match(line.strip())) if line.strip() else False


def align_block_boundaries(
    content: str,
    from_cp: int,
    to_cp: int,
    new_text: str,
) -> tuple[int, int, str]:
    """Widen a splice range so a BLOCK-level replacement lands on a block boundary.

    Returns the (possibly widened) range plus the (possibly padded) new_text. The
    range only ever grows over a run of spaces/tabs — the leftover separator — so
    the batch's non-overlap property is preserved (two ranges separated by zero
    whitespace cannot both grow into each other).

    # WHY: a replacement whose FIRST line is a Markdown block construct starts
    # on a block boundary; one whose LAST line is a block construct ends on one.
    # Why: incident doc-53e340da — a batch rewrite of a speech transcript replaced
    # fragments that the ORIGINAL separated by a plain space, so every new `## …`
    # heading was spliced onto the tail of the previous paragraph and rendered as
    # inline text. The model's new_string was correct; the surviving separator was
    # not. This is the projection over that class, NOT a per-construct patch.
    # A boundary that already exists (any newline) is left untouched — never
    # reformat deliberately-shaped content.
    """
    lines = new_text.split("\n")
    if _is_block_line(lines[0]):
        start = from_cp
        while start > 0 and content[start - 1] in " \t":
            start -= 1
        if start > 0 and content[start - 1] != "\n":
            from_cp = start
            new_text = "\n\n" + new_text
    if _is_block_line(lines[-1]):
        end = to_cp
        while end < len(content) and content[end] in " \t":
            end += 1
        if end < len(content) and content[end] != "\n":
            to_cp = end
            new_text = new_text + "\n\n"
    return from_cp, to_cp, new_text


def resolve_edit_range(
    content: str, old_string: str, *, full_rewrite_fraction: float,
) -> tuple[int, int, bool] | str:
    """Locate old_string in content (code points).

    Returns (from_cp, to_cp, folded) or an error tag. `folded` is True iff the
    normalized-fold retry produced the match — callers use it to apply the same
    fold to new_string (see the INVARIANT at the call sites). The spliced range is
    always ORIGINAL code points (offset map), so the doc's invisible chars inside the
    range are removed with it.

    `full_rewrite_fraction` is a REQUIRED argument, resolved by the ASYNC
    caller through settings.get("AGENT_FULL_REWRITE_FRACTION") (instance-settings
    override). The resolver never reads config itself — there is no sync default
    leg that could drift from the override-aware value.

    # INVARIANT(data-loss): full-rewrite ban. Why: user rule — never full rewrite;
    # pointwise edits only. A near-total old_string (>= the full-rewrite fraction of
    # the doc, or spanning offset 0 to end) is rejected structurally, not just by prompt.
    # The escape hatch is create_document (the tool error says so).

    Error tags:
      "not_found"    — 0 matches (raw AND folded).
      "ambiguous"    — >1 matches (model must add surrounding context).
      "full_rewrite" — old_string covers ~the whole document.
    """
    if not old_string:
        return "not_found"
    content_cp = len(content)
    old_cp = len(old_string)
    # Full-rewrite guard: fraction of doc OR exact full-span match.
    if old_cp >= full_rewrite_fraction * content_cp:
        return "full_rewrite"
    idx = content.find(old_string)
    if idx != -1:
        if content.find(old_string, idx + 1) != -1:
            return "ambiguous"
        # str.find operates on code points in Python 3.
        return (idx, idx + old_cp, False)
    # Raw miss → normalized-fold retry.
    # WHY: the model cannot perceive invisible chars (trailing spaces, NBSP, \r\n,
    # zero-width) even in verbatim content, so it sends a clean projection while the
    # doc holds the real chars. Fold BOTH sides and search the projection; splice the
    # ORIGINAL range via the offset map. Subsumes the old JSON.stringify \n de-escape.
    # WHY: old_string and new_string are folded together or not at all
    # (callers key on the flag). Fold relaxes CHARACTERS, never UNIQUENESS — a 0/>1
    # projected match returns the original tag, never a silently-promoted range.  Why: folding old+new together keeps resolver and presence-check aligned; fold relaxes characters but never uniqueness — a 0/>1 match must surface as the original tag, never a silently-promoted wrong range.
    # Why: doc-4da09e92 incident — `--- ` trailing-space separators looped forever.
    folded_old = _fold_old_string(old_string)
    if not folded_old:
        return "not_found"
    folded_content, starts = _fold_content(content)
    first = folded_content.find(folded_old)
    if first == -1:
        return "not_found"
    if folded_content.find(folded_old, first + 1) != -1:
        return "ambiguous"
    match_end = first + len(folded_old)
    from_cp = starts[first]
    to_cp = starts[match_end] if match_end < len(starts) else content_cp
    return (from_cp, to_cp, True)


def edit_range_error_detail(tag: str) -> str:
    """Human/agent-facing message for a resolve_edit_range error tag.

    # WHY: full_rewrite must steer the agent to a SEQUENCE of small pointwise edits,
    # NOT to create_document (which orphans the existing doc as a new one) and NOT to
    # "re-read and retry" the same whole-document old_string (fails the same guard).
    # Reason: restructuring an existing doc via successive small edit_document calls is
    # the intended good practice; create_document is for genuinely new documents only.
    """
    if tag == "full_rewrite":
        return (
            "old_string covers ~the whole document — edit_document is pointwise only. "
            "Restructure this document with a SEQUENCE of small edit_document calls, "
            "each replacing one unique passage; create_document is for new documents only."
        )
    return f"old_string {tag} — re-read the document and retry"


def _nearest_snippet(content: str, old_string: str, *, radius: int = 3, cap: int = 600) -> str | None:
    """The document lines most similar to old_string's first non-blank line, with
    surrounding context — a best-effort anchor for a blind-retry loop.

    # ARCH: the window was widened from
    # radius=1/cap=240 to radius=3/cap=600 so the returned snippet is large enough
    # to copy a UNIQUE old_string in one step. radius=1 (3 lines) was often too
    # small for a weak model to rebuild a unique match.
    """
    import difflib

    probe = next((ln for ln in (old_string or "").splitlines() if ln.strip()), old_string or "")
    if not probe.strip():
        return None
    lines = content.splitlines()
    if not lines:
        return None
    best = difflib.get_close_matches(probe.strip(), [ln.strip() for ln in lines], n=1, cutoff=0.3)
    if not best:
        return None
    idx = next((i for i, ln in enumerate(lines) if ln.strip() == best[0]), None)
    if idx is None:
        return None
    window = lines[max(0, idx - radius): idx + radius + 1]
    return "\n".join(window)[:cap]


def edit_miss_detail(content: str, old_string: str, tag: str) -> str:
    """Enriched edit-miss message (ergonomics 2A): append a nearest-match snippet on
    `not_found` and an add-context hint on `ambiguous`, so a weak model can break the
    blind-retry loop. Starts with edit_range_error_detail(tag) — a superset, so any
    substring check on the base message still holds.
    """
    base = edit_range_error_detail(tag)
    if tag == "not_found":
        near = _nearest_snippet(content, old_string)
        if near:
            return f"{base}. Closest text in the document (copy old_string verbatim from here):\n{near}"
    if tag == "ambiguous":
        return f"{base}. old_string matches multiple places — add surrounding context to make it unique."
    return base


def _splice_edit(
    content: str,
    from_cp: int,
    to_cp: int,
    original_text: str,
    new_text: str,
) -> tuple[str, str | None]:
    """Replace content[from_cp:to_cp] with new_text after verifying it matches original_text.

    Returns (new_content, error). On error, error is "stale" and new_content is the
    original content unchanged. Positions are Unicode code-point offsets — callers
    must convert from UTF-16 at the API boundary.

    # INVARIANT: never overwrite a range whose current text differs from the frozen
    # original. Concurrent edits to the same range MUST cause a 409, not a silent
    # overwrite.  Why: the frozen original is the optimistic-concurrency guard; if live text drifted (a concurrent edit), overwriting would clobber it silently — a 409 forces the caller to re-resolve against the new text.
    """
    if from_cp < 0 or to_cp < from_cp or to_cp > len(content):
        return content, "stale"
    current = content[from_cp:to_cp]
    if current != original_text:
        return content, "stale"
    return content[:from_cp] + new_text + content[to_cp:], None


def already_applied(content: str, old_string: str, new_string: str) -> tuple[str, int | None]:
    """Idempotent-skip probe (audit F1, plan "quizzical-mixing-marble").

    Tests whether `new_string` is present in the SAME folded projection
    `resolve_edit_range` uses for `old_string`. Called by the batch validation pass
    when old_string is not_found: a present-exactly-once new_string means a prior
    edit already applied this change, so the re-send is dropped (observable skip)
    rather than surfacing a stale 409.

    Returns:
      ("applied", cp)  — new_string present EXACTLY once; cp is the code-point
                         offset of the match start (for the skipped report).
      ("absent", None) — new_string not present (a wrong old_string whose intended
                         result does not exist → surface the enriched 409, never
                         silently swallow). Also the outcome for an EMPTY
                         new_string (a prior deletion is not locatable).
      ("ambiguous", None) — new_string present > 1 (cannot confirm which is the
                         prior apply → surface the 409).

    # INVARIANT(data-loss): presence is judged in the SAME fold as the resolver,
    # so it cannot disagree with it about 'present'.
    # Why: a false 'present' on re-apply would noop-skip an edit that never landed;
    # presence and resolution must share one fold (deescape + _fold_content applied
    # to new_string, symmetric with the resolver's old_string fold) so they agree.
    """
    if not new_string:
        # A prior deletion (empty new_string) is not locatable — surface the miss.
        return ("absent", None)
    folded_new, _ = _fold_content(deescape_control_chars(new_string))
    if not folded_new:
        return ("absent", None)
    folded_content, starts = _fold_content(content)
    first = folded_content.find(folded_new)
    if first == -1:
        return ("absent", None)
    if folded_content.find(folded_new, first + 1) != -1:
        return ("ambiguous", None)
    return ("applied", starts[first])
