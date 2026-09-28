"""Shared text-matching helpers for the search surfaces (UI route + agent).

Extracted verbatim from agent/search_exec.py so the UI search route
(routes/projects.py) can reuse the stop-word fan-out guard and the snippet-anchor
ladder WITHOUT importing the agent package (agent layering must not be imported
from routes). The agent re-imports these under its former private names — the
behavior is pinned by tests/backend/test_search_exec_units.py.

Also home (plan fewer-layers) to the surgical splice primitive and its typed
failure: `surgical_splice_text` + `AppliedUnverifiedError`, shared by the
live-session path (collab.events) and the no-session path
(agent.doc_state.route_document_edits). Keeping them here
makes this module the one leaf both collab and the agent package converge on —
it imports nothing from either (fastapi only, for the HTTPException base).
"""

import re

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
