"""Derived document outline (plan document-outline-in-structure-map).

The outline is a ROUTING HINT for the structure map's enumeration calls: raw
headings — never an LLM summary — so it cannot go stale against the body and
costs nothing to produce. Stored on the documents row (already rendered and
already degraded); the walk reads it as two plain columns.

# ARCH: derived and STORED, not computed per request. The walk's detail select
# reads two more columns instead of pulling `content` for every visible row.
# The write point is the embed job ABOVE _reembed — the embed call returns
# early when embeddings are unconfigured, and a project without embeddings
# must still get outlines. An outline failure never fails the embed job.
"""
import re

from deps import extract_headings

# WHY(300): the outline rides EVERY enumerated row, so the budget is per row —
# 300 chars × a wide subtree stays well inside the payload the walk already
# returns; anything a document needs beyond that is what read_document is for.
OUTLINE_MAX_CHARS: int = 300

# WHY(flat): indent characters are per-row token cost for an order the reader
# already has (the map's rows are depth-ordered); " · " separates levels.
OUTLINE_SEP: str = " · "

# Manual-numbering prefixes stripped from heading texts: dotted chains
# ("1.2.3 ") and chapter-word forms ("Глава 4. ").
_NUMBERING_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?|(?:Глава|Часть|Раздел|Section|Chapter|Part)\s+\d+[.:—–-]?)\s+"
)


def _clean_headings(title: str, headings: list[dict]) -> list[tuple[int, str]]:
    """Clean BEFORE any dropping — it is usually enough on its own: drop an H1
    equal to the document title, strip manual numbering, collapse consecutive
    duplicate heading texts."""
    title_norm = (title or "").strip().casefold()
    items: list[tuple[int, str]] = []
    for h in headings:
        text = (h.get("text") or "").strip()
        if h.get("level") == 1 and text.casefold() == title_norm:
            continue
        text = _NUMBERING_RE.sub("", text, count=1).strip()
        if not text:
            continue
        if items and items[-1][1].casefold() == text.casefold():
            continue
        items.append((h.get("level") or 1, text))
    return items


def build_outline(title: str, content: str) -> tuple[str, int]:
    """Pure: (outline, hidden) from a document's title + body.

    Degradation is structural, then positional, and always counted: over
    budget drop level 4, then level 3 (H1/H2 survive the ladder), then drop
    from the TAIL of the remaining list. Every dropped heading is counted
    into `hidden` — the walk's contract: everything withheld is COUNTED on
    its row.
    """
    items = _clean_headings(title, extract_headings(content or ""))
    if not items:
        return "", 0

    def render(rows: list[tuple[int, str]]) -> str:
        return OUTLINE_SEP.join(t for _, t in rows)

    outline = render(items)
    hidden = 0
    if len(outline) > OUTLINE_MAX_CHARS:
        for level in (4, 3):
            dropped = [i for i in items if i[0] == level]
            if not dropped:
                continue
            items = [i for i in items if i[0] != level]
            hidden += len(dropped)
            outline = render(items)
            if len(outline) <= OUTLINE_MAX_CHARS:
                break
        while items and len(outline) > OUTLINE_MAX_CHARS:
            items = items[:-1]
            hidden += 1
            outline = render(items)
    return outline, hidden


async def refresh_outline(entity_id: str) -> None:
    """Read title + content for one live document, write both outline fields.

    Stored NONE-normalized: an empty outline (nothing served) reads back NONE,
    and `outline_hidden` carries a count only when headings were actually
    dropped — the same only-what-is-populated rule the walk's row projection
    follows. Short-circuit: the embed job calls this on every debounce cycle,
    and most edits touch body text, not headings — a heading-identical body
    must not issue an UPDATE at all.
    """
    from db import get_db

    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id, title, content, outline, outline_hidden FROM documents "
        "WHERE meta::id(id) = $id AND deleted_at IS NONE",
        {"id": entity_id},
    )
    if not isinstance(rows, list) or not rows:
        return
    row = rows[0]
    outline, hidden = build_outline(row.get("title") or "", row.get("content") or "")
    new_o, new_h = outline or None, hidden or None
    if row.get("outline") == new_o and (row.get("outline_hidden") or None) == new_h:
        return
    await db.query(
        "UPDATE type::record('documents', $id) SET "
        "outline = $o, outline_hidden = $h",
        {"id": entity_id, "o": new_o, "h": new_h},
    )
