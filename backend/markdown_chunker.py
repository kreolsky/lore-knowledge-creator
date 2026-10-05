"""Pure markdown chunker — the recursive splitter behind doc_chunks.

The chunker
is a pure text algorithm — no database, no HTTP client, no event bus — and it is
importable without any of them (pinned by test_markdown_chunker_pure). The
embedding API client, the incremental re-embed machinery and the debounce
scheduler stay in embeddings.py. No `# SYSTEM:` marker — the embeddings entry
in embeddings.py keeps the single system catalog entry.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from deps import extract_headings
from transclusion_grammar import parse_target

# ─── Markdown chunker ────────────────────────────────────────────────────────
# WHY: recursive separator split. A section is split by progressively finer
#       separators (paragraph → line → sentence → word) descending ONLY when the
#       current unit overflows the soft target. A final character-split fallback
#       guarantees no chunk can exceed `input_max_chars`, so the embedding
#       provider can never reject an oversized input string (the ROOT CAUSE:
#       a one-line transcript became a single 48k chunk and the whole document
#       stayed out of the index). Offsets are carried POSITIONALLY through the
#       recursion — never re-located via str.find (which drifts on repeats).
# INVARIANT: every emitted chunk has len(content) <= input_max_chars.  Why: the recursive char-split fallback above enforces this so the embedding provider never rejects an oversized input.


@dataclass
class Chunk:
    ord: int
    heading: str | None
    content: str
    offset_start: int
    offset_end: int
    # S1 (D1): the ancestor heading TEXTS (root → this section), used only to build the
    # embedded breadcrumb ("Title > H1 > H2"). Not persisted to doc_chunks — recomputed
    # by the chunker on every re-embed. Default () keeps direct Chunk construction working.
    heading_path: tuple[str, ...] = ()


# A transclusion embed node `![alt](target)` — same shape as documents.service.
# Kept local to avoid importing the heavier documents.service module into the
# embedding hot path.
_EMBED_NODE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# Sentence end: one or more sentence-final punctuation, then whitespace. The
# boundary is accepted only if the token before it is not a known abbreviation
# or a single-letter initial (see _split_sentences).
_SENT_END_RE = re.compile(r"([.!?…]+)\s+")
_WS_RE = re.compile(r"\s+")
_PARA_RE = re.compile(r"\n\s*\n")
_LINE_RE = re.compile(r"\n")

# Common abbreviations whose trailing dot is NOT a sentence boundary. Modest set;
# D9 accepts cruder splits because overlap absorbs a misjudged boundary.
_ABBREVIATIONS = {
    # Russian
    "т.е", "т.д", "т.п", "г", "мин", "макс", "пр", "см", "им", "н", "в", "с",
    "ул", "д", "кв", "обл", "пос", "тов", "рис", "табл", "п", "к", "мм", "см",
    # English
    "mr", "mrs", "ms", "dr", "vs", "etc", "e.g", "i.e", "st", "no", "vol",
    "fig", "al", "inc", "ltd", "co", "jr", "sr", "approx", "min", "max",
}

_MIN_CHUNK_CHARS = 100      # a sub-minimum trailing chunk merges into its neighbour
_OVERLAP_FRACTION = 0.12    # ~12% of the soft target — whole sentences, not char slices


def chunk_markdown(
    text: str, *, max_chunk_chars: int, input_max_chars: int,
) -> list[Chunk]:
    """Split `text` into chunks of about `max_chunk_chars` (soft target), none
    longer than `input_max_chars` (hard ceiling). Callers resolve both from
    RETRIEVAL_CHUNK_MAX_CHARS / EMBEDDING_INPUT_MAX_CHARS via settings.get —
    the chunker stays pure (no settings, no DB)."""
    if not text.strip():
        return []
    # WHY: the soft target is capped at the hard ceiling. Why: both are live admin
    # knobs, and a leaf that fits the soft target is emitted whole — a target above
    # the ceiling would emit chunks the provider rejects (HTTP 400).
    max_chunk_chars = min(max_chunk_chars, input_max_chars)

    headings = extract_headings(text, max_level=6)
    sections = _build_sections(text, headings)

    chunks: list[Chunk] = []
    for heading_label, heading_path, s_start, s_end in sections:
        _chunk_section(
            text, s_start, s_end, heading_label, heading_path,
            max_chunk_chars, input_max_chars, chunks,
        )

    for i, c in enumerate(chunks):
        c.ord = i
    return chunks


def _build_sections(
    text: str, headings: list[dict],
) -> list[tuple[str | None, tuple[str, ...], int, int]]:
    """Section spans `(heading_label, heading_path, content_start, content_end)`.

    Each heading opens a section running to the next heading of any level; preamble
    before the first heading is its own (no heading context). The heading TEXT is carried
    in `boundaries` (not recovered via `headings[idx]`): the `line_idx < len(line_starts)`
    guard can drop an entry, and positional indexing back into `headings` would then
    silently produce wrong breadcrumbs for every section after the drop.
    """
    # Map 1-based heading line numbers → absolute char offsets (robust to the
    # fence-aware parse the shared parser performs).
    line_starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)

    boundaries: list[tuple[int, int, str]] = []  # (level, abs_offset, heading_text)
    for h in headings:
        line_idx = h["line"] - 1
        if line_idx < len(line_starts):
            boundaries.append((h["level"], line_starts[line_idx], h["text"]))

    sections: list[tuple[str | None, tuple[str, ...], int, int]] = []
    cursor = 0
    stack: list[tuple[int, str]] = []  # ancestor (level, text) chain — root → current
    for idx, (level, h_off, title) in enumerate(boundaries):
        if h_off > cursor:
            sections.append((None, (), cursor, h_off))  # preamble — no heading context
        # Section body starts after the heading line.
        nl = text.find("\n", h_off)
        content_start = (nl + 1) if nl != -1 else len(text)
        content_end = boundaries[idx + 1][1] if idx + 1 < len(boundaries) else len(text)
        # Ancestor stack (D5): pop deeper-or-equal levels, then push this heading, so a
        # chunk under "### C" (with parents "# A", "## B") carries path ("A","B","C").
        # A later sibling "# D" pops B and C — it does not inherit them.
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        heading_path = tuple(t for _, t in stack)
        sections.append((f"{'#' * level} {title}", heading_path, content_start, content_end))
        cursor = content_end
    if cursor < len(text):
        sections.append((None, (), cursor, len(text)))

    if not sections:
        sections = [(None, (), 0, len(text))]
    return sections


def _chunk_section(
    text: str, s_start: int, s_end: int,
    heading_label: str | None, heading_path: tuple[str, ...],
    max_chars: int, hard_max: int, result: list[Chunk],
) -> None:
    # Skip empty sections (e.g. a heading immediately followed by another heading).
    if text[s_start:s_end].strip() == "":
        return

    # Recursive split into leaf spans, each <= max_chars (or a hard char-split
    # piece <= hard_max). Leaves carry absolute offsets.
    leaves: list[tuple[int, int]] = []
    _split_into_leaves(text, s_start, s_end, max_chars, hard_max, leaves)
    if not leaves:
        return

    overlap_chars = max(_MIN_CHUNK_CHARS, int(max_chars * _OVERLAP_FRACTION))

    # Greedy pack leaves into chunk spans, whole-sentence overlapping neighbours.
    # WHY: bound by the SPAN width (leaves[j].end - leaves[i].start), NOT the sum  Why: emitted content is text[start:end] spanning the inter-leaf separators, so summed leaf length underestimates and chunks could overflow the ceiling.
    # of leaf-text lengths — the emitted content is text[start:end], which includes the
    # separators sitting between leaves. Summing leaf text alone let many tiny paragraphs
    # pack into one chunk whose separator-inflated content exceeded the hard ceiling.
    spans: list[tuple[int, int]] = []  # (abs_start, abs_end)
    n = len(leaves)
    i = 0
    while i < n:
        j = i
        while j < n and (j == i or leaves[j][1] - leaves[i][0] <= max_chars):
            j += 1
        chunk_start = leaves[i][0]
        chunk_end = leaves[j - 1][1]
        spans.append((chunk_start, chunk_end))
        if j >= n:
            break
        # Overlap: restart the next chunk ~overlap_chars into the tail of this one.
        nxt = _overlap_start(leaves, i, j, overlap_chars)
        i = nxt if nxt > i else j

    _merge_tiny_tail(spans, _MIN_CHUNK_CHARS, hard_max)
    _emit_chunks(text, spans, heading_label, heading_path, result)


def _split_into_leaves(
    text: str, abs_s: int, abs_e: int,
    max_chars: int, hard_max: int, out: list[tuple[int, int]],
) -> None:
    length = abs_e - abs_s
    if length <= max_chars:
        out.append((abs_s, abs_e))
        return

    segment = text[abs_s:abs_e]
    # Coarse → fine separators. The first that yields >1 piece wins; oversized
    # children are recursed (and descend to a finer separator on the next call).
    for splitter in (_split_paragraphs, _split_lines, _split_sentences, _split_words):
        parts = splitter(segment, abs_s)
        if len(parts) > 1:
            for s, e in parts:
                if e - s > 0:
                    _split_into_leaves(text, s, e, max_chars, hard_max, out)
            return

    # No separator splits this overflowed span (e.g. a base64 blob with no
    # whitespace): hard character-split at the provider-safe ceiling.
    for k in range(abs_s, abs_e, hard_max):
        out.append((k, min(k + hard_max, abs_e)))


def _split_offsets(segment: str, base: int, pattern: re.Pattern) -> list[tuple[int, int]]:
    """Text spans BETWEEN separator matches (separators dropped from the leaves;
    they survive in the original text and are included when a chunk spans several
    leaves via its absolute offsets)."""
    pieces: list[tuple[int, int]] = []
    last = 0
    for m in pattern.finditer(segment):
        if m.start() > last:
            pieces.append((base + last, base + m.start()))
        last = m.end()
    if last < len(segment):
        pieces.append((base + last, base + len(segment)))
    return pieces


def _split_paragraphs(segment: str, base: int) -> list[tuple[int, int]]:
    return _split_offsets(segment, base, _PARA_RE)


def _split_lines(segment: str, base: int) -> list[tuple[int, int]]:
    return _split_offsets(segment, base, _LINE_RE)


def _split_words(segment: str, base: int) -> list[tuple[int, int]]:
    return _split_offsets(segment, base, _WS_RE)


def _split_sentences(segment: str, base: int) -> list[tuple[int, int]]:
    """Sentence-boundary split with a modest abbreviation guard.

    A `[.!?…]+\\s+` match is a boundary only if the token before it is neither a
    known abbreviation nor a single-letter initial. Crude by design (D9): a missed
    boundary only makes one leaf longer, and overlap absorbs the seam.
    """
    pieces: list[tuple[int, int]] = []
    last = 0
    for m in _SENT_END_RE.finditer(segment):
        punct_end = m.start() + len(m.group(1))  # include the punctuation in the leaf
        token = _token_before(segment, m.start())
        if _is_abbreviation(token):
            continue
        pieces.append((base + last, base + punct_end))
        last = m.end()
    if last < len(segment):
        pieces.append((base + last, base + len(segment)))
    return pieces


def _token_before(segment: str, pos: int) -> str:
    """The trailing alnum token immediately before `pos` (for the abbreviation guard)."""
    j = pos
    while j > 0 and segment[j - 1].isspace():
        j -= 1
    k = j
    while k > 0 and (segment[k - 1].isalnum() or segment[k - 1] in ".-"):
        k -= 1
    return segment[k:j]


def _is_abbreviation(token: str) -> bool:
    if not token:
        return False
    core = token.rstrip(".").lower()
    if core in _ABBREVIATIONS:
        return True
    # Single-letter initial (e.g. "А.", "J.") — not a sentence end.
    return len(core) == 1 and token[:1].isalpha()


def _overlap_start(
    leaves: list[tuple[int, int]], i: int, j: int, overlap_chars: int,
) -> int:
    """Index into `leaves` where the tail (>= overlap_chars) of leaves[i:j] begins,
    so the next chunk repeats that tail as overlap."""
    acc = 0
    for k in range(j - 1, i - 1, -1):
        acc += leaves[k][1] - leaves[k][0]
        if acc >= overlap_chars:
            return k
    return i


def _merge_tiny_tail(spans: list[tuple[int, int]], min_chars: int, hard_max: int) -> None:
    # A sub-minimum trailing chunk is retrieval noise — fold it into the previous chunk.
    # May push that chunk slightly over the soft target, but never near the hard ceiling
    # (the tail is tiny).
    # Respect the hard ceiling: if merging would push the previous span past hard_max,
    # leave the tiny tail as its own (sub-minimum) chunk. The ceiling is a provider
    # contract (an oversized string is rejected — the root cause the recursive splitter
    # was built to eliminate); the minimum size is only a quality preference, so the
    # ceiling wins and only the pathological no-separator path can reach this conflict.
    if len(spans) < 2:
        return
    if spans[-1][1] - spans[-1][0] >= min_chars:
        return
    prev_start = spans[-2][0]
    tail_end = spans[-1][1]
    if tail_end - prev_start > hard_max:
        return
    spans[-2] = (prev_start, tail_end)
    spans.pop()


def _emit_chunks(
    text: str, spans: list[tuple[int, int]],
    heading_label: str | None, heading_path: tuple[str, ...],
    result: list[Chunk],
) -> None:
    # WHY: (offset_start, offset_end) is the SOURCE SPAN the chunk was derived
    # from, NOT a slice equal to content.
    # Why: content is the PROJECTION _strip_transclusions(text[start:end]).strip() — it
    # drops transclusion embed nodes (![alt](target)) and leading/trailing whitespace, and
    # removing an interior node makes the source non-contiguous, so contiguous offsets
    # equal to content cannot be re-derived (offset_end - offset_start != len(content)
    # whenever a node or padding is removed). The offsets ARE tightened to the
    # post-.strip() non-whitespace bounds (the computable part — what fixes the scroll
    # target in Sources.tsx, which seeks offset_start); interior transclusion nodes
    # remain in the span as the accepted residual. The projection
    # (_strip_transclusions(text[offset_start:offset_end]).strip() == content) is the
    # pinned contract.
    for idx, (start, end) in enumerate(spans):
        raw = text[start:end]
        content = _strip_transclusions(raw).strip()
        if not content:
            continue
        # Tighten to the post-.strip() non-whitespace bounds. Operate on `raw` (the
        # original slice) so the offsets stay valid in the source text; the transclusion-
        # stripped string's coordinates do not map back once an interior node is removed.
        lead = len(raw) - len(raw.lstrip())
        trail = len(raw) - len(raw.rstrip())
        offset_start = start + lead
        offset_end = end - trail
        # The section heading appears ONCE — on the first chunk of the section.
        # (Fixes the old alternating dedup: comparing against result[-1].heading
        # made the label flicker on/off once a None appeared.)
        heading = heading_label if idx == 0 else None
        result.append(Chunk(
            ord=0,  # renumbered by the caller
            heading=heading,
            content=content,
            offset_start=offset_start,
            offset_end=offset_end,
            # Every chunk in the section shares the ancestor path — the breadcrumb
            # gives each chunk a subject, not just the first.
            heading_path=heading_path,
        ))


def _strip_transclusions(text: str) -> str:
    """Drop transclusion embed nodes (![alt](target)) — they are pointers, not prose,
    and embedding their target id as a literal URL string is noise (D6). A real
    external image / data URI is not a transclusion and is left intact."""
    def _repl(m: re.Match) -> str:
        return "" if parse_target(m.group(2)) is not None else m.group(0)
    return _EMBED_NODE_RE.sub(_repl, text)


def _breadcrumb_text(title: str, chunk: Chunk) -> str:
    """The string actually sent to the embedding model (D1): the document title and
    ancestor heading path as a breadcrumb, then the bare chunk content. Callers still
    receive/store `chunk.content` unchanged — only the vector is computed from this.

    WHY a breadcrumb and not a second heading vector: two vectors force a per-query
    mixing weight that depends on a query type we do not know. Prefixing the subject
    into the same vector lets cosine do the mixing.
    """
    path = [p for p in (title, *chunk.heading_path) if p]
    if not path:
        return chunk.content
    return " > ".join(path) + "\n\n" + chunk.content


# ─── Incremental re-embed (D7/D11) ───────────────────────────────────────────
# A content_hash over the EMBEDDED text (breadcrumb included) lets a re-embed SKIP the
# embedding API call for chunks whose text did not change. The hash saves the EMBEDDING,
# not the DB write: a chunk whose text is unchanged but whose ord/offsets moved still
# gets an UPDATE in place (D7). A row with no hash (pre-S3) is never reused — the first
# re-embed after deploy re-embeds it (self-heal), then stores the hash for next time (D11).


def _chunk_hash(title: str, chunk: Chunk, model: str) -> str:
    """sha256 over the model name + the embedded text (breadcrumb + content).

    Two chunks with the same content but a different heading path hash differently, so
    renaming a section or the document title correctly invalidates its chunks (the vector
    genuinely changed). Stable across a no-op re-embed (same model + title + path + content).

    The EMBEDDING_MODEL NAME is mixed in so a model swap invalidates every vector —
    without it the table silently reuses the old model's geometry under a new model's
    name and two models' geometry coexist in one column. Hash the NAME only, not the
    detected dimension: the dimension is not known before the first API call, and a
    dimension change without a name change is not a case this provider produces. Cost:
    the model id enters the hash, so every existing row's hash changes once and the next
    re-embed of each document is a full re-embed (the self-heal path a missing hash
    already takes on its first re-embed — the parent plan carries the corpus-wide
    re-embed as an accepted risk).

    `model` is a REQUIRED argument, resolved by the async caller (embeddings
    reads the override-aware value) — the hash never reads config itself.
    """
    payload = f"{model}\x00{_breadcrumb_text(title, chunk)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
