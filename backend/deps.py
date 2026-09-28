"""Small shared helpers with no natural home in a focused module.

Formerly a re-export shim for auth/access/cascade/mentions (W4 deleted that shim:
those symbols now have ONE import source — `from auth import get_current_user`,
and so on). What remains are three genuine cross-cutting helpers used by several
routes/services that do not belong to auth/access/mentions.
"""
# ARCH: Shared helpers only — NOT a re-export shim. Import auth/access/cascade/
#   mentions symbols from their own module; do not add them back here.

import re
from datetime import datetime

# ─── Shared constants ────────────────────────────────────────────────────────

MAX_WS_MESSAGE_SIZE = 1_000_000


# ─── Helpers ─────────────────────────────────────────────────────────────────


def json_safe(d: dict) -> dict:
    """Convert datetime values to ISO strings for JSON serialization."""
    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in d.items()}


def extract_headings(content: str, *, max_level: int = 4) -> list[dict]:
    """Parse markdown content to extract headings (H1–H`max_level`), skipping fenced code blocks.

    WHY fence detection: code blocks often contain `# comments` that look like
    headings — without skipping fences, the TOC would be polluted with false positives.
    WHY max_level: defaults to 4 (the TOC cap — its sole historical reason). The
    chunker passes max_level=6 so an H5/H6 still opens an indexing section; every
    other consumer keeps the default and is unchanged.
    """
    items: list[dict] = []
    lines = content.split("\n")
    in_fenced = False
    fence_char = ""
    fence_len = 0

    for i, line_text in enumerate(lines):
        fence_match = re.match(r"^(`{3,}|~{3,})", line_text)
        if fence_match:
            if not in_fenced:
                in_fenced = True
                fence_char = fence_match.group(1)[0]
                fence_len = len(fence_match.group(1))
            elif line_text[0] == fence_char and len(fence_match.group(1)) >= fence_len:
                in_fenced = False
                fence_char = ""
                fence_len = 0
            continue
        if in_fenced:
            continue

        heading_match = re.match(rf"^(#{{1,{max_level}}})\s+(.+)$", line_text)
        if heading_match:
            items.append({
                "level": len(heading_match.group(1)),
                "text": heading_match.group(2),
                "line": i + 1,
            })
    return items

