"""Shared text-matching helpers for the search surfaces (UI route + agent).

The UI search route (routes/projects.py) reuses the stop-word fan-out guard and
the snippet-anchor ladder WITHOUT importing the agent package (agent layering must
not be imported from routes). The agent re-imports these under private names — the
behavior is pinned by tests/backend/test_search_exec_units.py.

Also home to the surgical splice primitive and its typed failure:
`surgical_splice_text` + `AppliedUnverifiedError`, shared by the live-session path
(collab.events) and the no-session path (agent.doc_state.route_document_edits);
and to its wholesale twin `minimal_splice_text`, shared by ydoc_store.set_content
and the wholesale branch of collab.events.apply_external_content_change. Keeping them here
makes this module the one leaf both collab and the agent package converge on —
it imports nothing from either (fastapi only, for the HTTPException base).
"""

import re
from difflib import SequenceMatcher

from fastapi import HTTPException

# Russian stop-word set for the FTS operand. SurrealDB FULLTEXT matches ANY token
# (OR semantics), so a natural-language query fan-outs to every doc sharing a
# particle/conjunction — `'что известно про замок и его владельца'` matched 111 of
# 240 docs (46%) on the dev project, all in the default rank tier (S1.0 measurement,
# 2026-08). Stripping the high-frequency function words cut that to 6 with no harm to
# clean keyword queries. Kept short on purpose — it is a fan-out guard, not a parser.
RU_STOP_WORDS = frozenset("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по
только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если
уже или ни быть был него до вас опять уж вам ведь там потом себя ничего ей может
они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже
себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один
почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец два об
другой хоть после над больше тот через эти нас про всего них какая много разве три
эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более
всегда конечно всю между
""".split())


def strip_stop_words(query: str) -> str:
    """Drop Russian stop words from `query` for the FTS operand. Returns '' when the
    query is ONLY stop words (the caller then degrades to the raw query)."""
    toks = [t for t in re.split(r"\s+", (query or "").lower()) if t and t not in RU_STOP_WORDS]
    return " ".join(toks)


def fts_operand(q: str) -> str:
    """The FULLTEXT operand for a UI-route query: lowercase `\\W+`-split tokens with
    stop words and sub-3-char tokens dropped, space-joined.

    '' ⇒ the caller runs the literal contains scan instead (a deliberate mode, not
    a degradation): punctuation-heavy queries (`ИИ-1.2` → tokens ии/1/2, all <3)
    have no meaningful FTS form, and every token they COULD match fans out.
    """
    toks = [
        t for t in re.split(r"\W+", (q or "").lower())
        if len(t) >= 3 and t not in RU_STOP_WORDS
    ]
    return " ".join(toks)


def snippet_anchor(body_lower: str, qlower: str, prefix_lengths: tuple[int, ...] = (5, 4)) -> int:
    """Find a window anchor for the snippet when the match may NOT be verbatim.

    FTS finds inflected/prefix forms whose query string is absent from the body, so
    `body.find(query)` is -1 and the old code fell back to the document head — which
    reads as a wrong hit. Try, in order: the whole query; the longest query token of
    ≥3 chars; that token's leading prefixes (covers the stem-only hit where neither
    the query nor any token appears verbatim, e.g. «замка»→«замок»); then -1 (head).
    """
    pos = body_lower.find(qlower)
    if pos >= 0:
        return pos
    tokens = sorted((t for t in re.split(r"\W+", qlower) if len(t) >= 3), key=len, reverse=True)
    for tok in tokens:
        pos = body_lower.find(tok)
        if pos >= 0:
            return pos
        for n in prefix_lengths:
            if len(tok) > n:
                pos = body_lower.find(tok[:n])
                if pos >= 0:
                    return pos
    return -1


def anchor_pattern(
    body_lower: str, qlower: str, prefix_lengths: tuple[int, ...] = (5, 4),
) -> str | None:
    """The literal substring `snippet_anchor`'s ladder would stop on, or None.

    Same ladder as snippet_anchor but returns the PATTERN string so callers can
    count occurrences (match_count) and build the snippet around the very text
    that anchored the window — position and pattern cannot drift apart.
    """
    if qlower in body_lower:
        return qlower
    tokens = sorted((t for t in re.split(r"\W+", qlower) if len(t) >= 3), key=len, reverse=True)
    for tok in tokens:
        if tok in body_lower:
            return tok
        for n in prefix_lengths:
            if len(tok) > n and tok[:n] in body_lower:
                return tok[:n]
    return None


class AppliedUnverifiedError(HTTPException):
    """Raised when the mutation failed AFTER the write was reported as applied.

    # ARCH: the direct apply path needs the
    # typed "content mutation shifted by a concurrent edit" signal that
    # `surgical_splice_text` raises — a concurrent editor edit shifting the text
    # between resolve and write would otherwise delete the WRONG byte range
    # (corruption). Fail-stop, never corrupt. Moved from
    # agent.apply_edits_resolver (plan fewer-layers) so it lives
    # beside the primitive that raises it.
    """

    def __init__(self, detail: str = "Content update failed after it was reported as applied; refresh to verify"):
        super().__init__(status_code=500, detail=detail)


def surgical_splice_text(text, from_cp: int, to_cp: int, new_text: str,
                         original_text: str | None = None) -> None:
    """cp→UTF-8-byte del+insert on a pycrdt Text IN PLACE.

    The single source of truth for the surgical slice primitive, shared by
    the live-session path (`apply_external_content_change` surgical branch) and the
    no-session path (`route_document_edits`). pycrdt `Text` indexes in UTF-8 bytes;
    `from_cp`/`to_cp` are Unicode code points, so the prefix/slice are encoded once
    to derive the byte offsets (the slice is encoded ONCE, not the whole prefix
    twice).

    # ARCH (TOCTOU corruption guard): when `original_text` is supplied,
    # verify `current[from_cp:to_cp] == original_text` AFTER re-reading the live text
    # (the caller holds `_write_lock` / has just read synchronously). A concurrent
    # editor edit shifting the text between the proposal resolver's read and this
    # write would otherwise delete the WRONG byte range (corruption). On mismatch,
    # raise `AppliedUnverifiedError` — fail-stop, NOT corrupt. The existing
    # `_apply_edit_proposal` `except Exception` converts it to a 500 "refresh to
    # verify", the same trade the mark-first invariant already makes. `None`
    # (wholesale callers / legacy tests) skips the check.
    """
    current = str(text)
    fb = len(current[:from_cp].encode("utf-8"))
    tb = fb + len(current[from_cp:to_cp].encode("utf-8"))
    if original_text is not None and current[from_cp:to_cp] != original_text:
        raise AppliedUnverifiedError(
            "pinned-region edit range shifted by a concurrent edit — refresh to verify"
        )
    if tb > fb:
        del text[fb:tb]
    if new_text:
        text.insert(fb, new_text)


# WHY: above this size a replaced line block is swapped whole instead of diffed
# char-wise — SequenceMatcher is quadratic in the worst case and the caller holds
# `_write_lock`; an anchor inside a block rewritten that heavily is lost either way.
_CHAR_DIFF_CAP = 2000
# WHY: diff + apply cost ~0.85 ms per changed block on a 300k-char doc (pycrdt walks
# its item list per op, SequenceMatcher rescans per matching block), all under
# `_write_lock`. Past this many changed-line runs the trimmed middle is replaced
# whole — bounded lock time over keeping anchors inside a rewrite that dense.
_MAX_CHANGED_RUNS = 64


def _common_len(a: str, b: str, *, suffix: bool) -> int:
    """Length of the common prefix (or suffix) of a and b, by binary search over
    C-level slice compares instead of a per-char Python loop."""
    lo, hi = 0, min(len(a), len(b))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        same = a[len(a) - mid:] == b[len(b) - mid:] if suffix else a[:mid] == b[:mid]
        lo, hi = (mid, hi) if same else (lo, mid - 1)
    return lo


def _changed_runs(lines: list[str], other: list[str]) -> int:
    """Runs of consecutive lines absent from `other` — an O(n) lower bound on the
    number of change blocks a line diff would produce."""
    present = set(other)
    runs, prev = 0, False
    for line in lines:
        cur = line not in present
        runs += cur and not prev
        prev = cur
    return runs


def _diff_spans(a: str, b: str) -> list[tuple[int, int, str]]:
    """Ascending, non-overlapping `(from_cp, to_cp, replacement)` spans turning a into b."""
    pre = _common_len(a, b, suffix=False)
    suf = _common_len(a[pre:], b[pre:], suffix=True)
    a_mid, b_mid = a[pre:len(a) - suf], b[pre:len(b) - suf]
    a_lines = a_mid.splitlines(keepends=True)
    b_lines = b_mid.splitlines(keepends=True)
    if max(_changed_runs(a_lines, b_lines), _changed_runs(b_lines, a_lines)) > _MAX_CHANGED_RUNS:
        return [(pre, len(a) - suf, b_mid)]
    a_at = [pre]
    for line in a_lines:
        a_at.append(a_at[-1] + len(line))
    spans: list[tuple[int, int, str]] = []
    opcodes = SequenceMatcher(None, a_lines, b_lines, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        old, new = a_mid[a_at[i1] - pre:a_at[i2] - pre], "".join(b_lines[j1:j2])
        if tag != "replace" or max(len(old), len(new)) > _CHAR_DIFF_CAP:
            spans.append((a_at[i1], a_at[i2], new))
            continue
        for ctag, c1, c2, d1, d2 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if ctag != "equal":
                spans.append((a_at[i1] + c1, a_at[i1] + c2, new[d1:d2]))
    return spans


def minimal_splice_text(text, new_content: str) -> None:
    """Turn a pycrdt Text into `new_content` IN PLACE by splicing only what changed.

    The wholesale writers' primitive (restore, re-upload, REST update, workers):
    the old delete-all + insert re-created every item, so every Yjs RelativePosition
    anchored in the text (note anchors, pinned regions) was orphaned. Here the
    common prefix/suffix is trimmed, the middle diffed line-wise, and a replaced
    line block diffed char-wise when both sides fit `_CHAR_DIFF_CAP` — so unchanged
    text keeps its items and every anchor on it survives. A middle with more than
    `_MAX_CHANGED_RUNS` changed-line runs is replaced whole (anchors inside it lost).

    Computes the spans from ONE read of the text and applies them right-to-left in
    one transaction with no await in between, so the offsets cannot go stale. cp
    offsets become UTF-8 byte offsets (pycrdt indexing) in a single forward pass.
    """
    current = str(text)
    if current == new_content:
        return
    byte_spans: list[tuple[int, int, str]] = []
    cp = bpos = 0
    for from_cp, to_cp, replacement in _diff_spans(current, new_content):
        fb = bpos + len(current[cp:from_cp].encode("utf-8"))
        tb = fb + len(current[from_cp:to_cp].encode("utf-8"))
        byte_spans.append((fb, tb, replacement))
        cp, bpos = to_cp, tb
    with text.doc.transaction():
        for fb, tb, replacement in reversed(byte_spans):
            if tb > fb:
                del text[fb:tb]
            if replacement:
                text.insert(fb, replacement)


def _section_end_offset(content: str, section: str) -> int:
    """Code-point offset marking the END of one heading section's content.

    The section starts at the heading whose text matches `section` (exact, then
    case-insensitive) and extends to the line BEFORE the next heading at the SAME
    or a HIGHER level (or end of document). The returned offset is the position
    right after the last non-blank line of that section — the append point.

    Reuses the shared markdown heading parser (deps.extract_headings); do NOT
    write a new one. Raises LookupError when no heading matches `section`.

    Moved from routes/tool_api/edits.py (plan fewer-layers): a PURE heading-section
    helper consumed by both the append tool (routes.tool_api.edits) and the
    table-writes section append — its old home forced table_writes to import UP
    into routes.tool_api, closing an import cycle. Lives HERE (not in the agent
    package's edit_primitives) because routes.tool_api may not import private
    routes.chat names (module-boundary test).
    """
    from deps import extract_headings

    headings = extract_headings(content)
    target = next((h for h in headings if h["text"] == section), None)
    if target is None:
        lowered = section.lower()
        target = next((h for h in headings if h["text"].lower() == lowered), None)
    if target is None:
        raise LookupError(f"Section heading not found: {section!r}")

    level = target["level"]
    idx = headings.index(target)
    next_line = next(
        (h["line"] for h in headings[idx + 1:] if h["level"] <= level), None,
    )
    lines = content.split("\n")
    start = target["line"] - 1  # 0-based heading line
    end_exclusive = (next_line - 1) if next_line is not None else len(lines)
    # Last non-blank body line; fall back to the heading line itself (empty section).
    last = start
    for i in range(start, min(end_exclusive, len(lines))):
        if lines[i].strip():
            last = i
    offset = 0
    for i in range(last):
        offset += len(lines[i]) + 1  # content + its trailing "\n"
    return offset + len(lines[last])
