"""Public link previews — Open Graph cards for /docs/<id> URLs.

A link-preview bot (Telegram, WhatsApp, Slack, …) does not execute JS, so the
SPA shell it receives at /docs/<id> carries no per-document metadata and every
pasted link renders the same generic title. nginx (frontend/nginx.conf) routes
known bot UAs on /docs/<id> to this endpoint, which answers a tiny HTML page
with og:*/twitter:* tags: the document's title and excerpt when it is
published, a generic Lore card when it is not. Humans keep getting the
unchanged SPA.

Enters through SYSTEM: public-share's single funnel (`_require_doc`) exactly
like routes/public_notes.py — there is no other read path here.

ARCH(no-rate-limit) reconciliation: this route *inherits* the router's
no-rate-limit decision (routes/public_share.py:43) — it is one funnel call +
one row read, cheaper than the public document GET a human pays. Why pin: a
reflexive "anonymous endpoint ⇒ must rate-limit" PR would re-introduce the
429-on-honest-readers harm for no security gain.

INVARIANT(security): the generic card contains NOTHING derived from the
document. Why: the preview is anonymous — returning the title of a private
doc by uuid would be a read path around the funnel and an existence oracle.
Only the id the caller sent is echoed (in the refresh target and og:url), so
unpublished, unknown, deleted, revoked and system-subtree ids yield bodies
identical after the requested id is replaced by a placeholder.
"""
from __future__ import annotations

import html
import logging
import re

from fastapi import APIRouter, HTTPException, Request, Response

from db import get_db
from deps import FENCE_LINE_RE, extract_headings
from routes.public_share import _capture_public_content, _require_doc

router = APIRouter()
logger = logging.getLogger(__name__)

_SITE_NAME = "Lore"
# Same tagline as the SPA shell's <title> (frontend/index.html) — a bot sees one
# or the other for a /docs/<id> URL. Pinned equal by test_public_preview.py.
_GENERIC_DESCRIPTION = "Lore – Knowledge Creator"
_IMAGE_PATH = "/og-default.png"
_IMAGE_WIDTH = 1200
_IMAGE_HEIGHT = 630
_EXCERPT_MAX = 200
# Revoking a share stops new cards within 5 minutes; caches inside the
# messengers are out of our control.
_CACHE_CONTROL = "public, max-age=300"


def _origin(request: Request) -> str:
    """Absolute origin: X-Forwarded-Proto first hop (same rule as auth's
    _browser_scheme_is_https) + the Host header. No config variable."""
    forwarded = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded.split(",")[0].strip().lower() or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


def _lines_outside_fences(content: str) -> list[str]:
    """Lines not inside fenced code blocks (same fence rules as extract_headings)."""
    kept: list[str] = []
    in_fenced = False
    fence_char = ""
    fence_len = 0
    for line in content.split("\n"):
        fence_match = FENCE_LINE_RE.match(line)
        if fence_match:
            if not in_fenced:
                in_fenced = True
                fence_char = fence_match.group(1)[0]
                fence_len = len(fence_match.group(1))
            elif line[0] == fence_char and len(fence_match.group(1)) >= fence_len:
                in_fenced = False
                fence_char = ""
                fence_len = 0
            continue
        if in_fenced:
            continue
        kept.append(line)
    return kept


def _plain_text(content: str) -> str:
    """Markdown → flat plain text: fences dropped, embed/link syntax and
    markdown markers stripped, whitespace collapsed."""
    text = "\n".join(_lines_outside_fences(content))
    # Lore embeds / markdown images / links: keep the visible label only.
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # Inline code, bold, italic, strikethrough markers.
    text = re.sub(r"(`+|\*+|_+|~~)", "", text)
    # Heading marks, blockquotes, list bullets at line starts.
    text = re.sub(r"^\s*(#{1,6}|>|[-*+]|\d+\.)\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()


def _excerpt(content: str) -> str:
    """≤ _EXCERPT_MAX chars on a word boundary, ellipsized when cut."""
    plain = _plain_text(content)
    if len(plain) <= _EXCERPT_MAX:
        return plain
    head = plain[:_EXCERPT_MAX]
    boundary = head.rfind(" ")
    if boundary < _EXCERPT_MAX // 2:
        boundary = _EXCERPT_MAX  # no sane boundary — hard cut
    return head[:boundary].rstrip() + "…"


def _title_or_fallback(title: str, content: str) -> str:
    """Document title; empty → first heading; else the site name."""
    if title.strip():
        return title.strip()
    headings = extract_headings(content)
    return headings[0]["text"] if headings else _SITE_NAME


async def _title_and_excerpt(document_id: str) -> tuple[str, str]:
    """(title, excerpt) from live text; on the capture 503, DB content.

    Ruling (plan): preview-only DB fallback — the human reader path keeps its
    503 (no silent degradation for readers); a bot card must not become a
    cached error. Cost if wrong: a stale excerpt in one card.
    """
    db = await get_db()
    rows = await db.query(
        "SELECT id, title, content FROM documents "
        "WHERE id = type::record('documents', $id) AND deleted_at IS NONE",
        {"id": document_id},
    )
    if not rows:
        # Deleted between the funnel and this read — same treatment as unpublished.
        raise HTTPException(status_code=404, detail="Document not found")
    row = rows[0]
    db_content = row.get("content") or ""
    try:
        content, _tables_json = await _capture_public_content(document_id, db_content)
    except HTTPException as e:
        if e.status_code != 503:
            raise
        logger.warning("preview serving DB content for public doc %s", document_id)
        content = db_content
    return _title_or_fallback(row.get("title") or "", content), _excerpt(content)


def _preview_html(
    *, document_id: str, origin: str, title: str, description: str, og_type: str,
) -> str:
    """The card page. Every value is HTML-escaped — titles and content are
    user-authored."""
    url = f"{origin}/docs/{document_id}"
    image = f"{origin}{_IMAGE_PATH}"
    refresh = f"0;url=/docs/{document_id}/"  # trailing slash: see module docstring
    esc = lambda s: html.escape(s, quote=True)  # noqa: E731
    return (
        "<!DOCTYPE html>\n<html><head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{esc(title)}</title>\n"
        f'<meta name="description" content="{esc(description)}">\n'
        f'<meta property="og:type" content="{esc(og_type)}">\n'
        f'<meta property="og:title" content="{esc(title)}">\n'
        f'<meta property="og:description" content="{esc(description)}">\n'
        f'<meta property="og:site_name" content="{esc(_SITE_NAME)}">\n'
        f'<meta property="og:url" content="{esc(url)}">\n'
        f'<meta property="og:image" content="{esc(image)}">\n'
        f'<meta property="og:image:width" content="{_IMAGE_WIDTH}">\n'
        f'<meta property="og:image:height" content="{_IMAGE_HEIGHT}">\n'
        f'<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta http-equiv="refresh" content="{esc(refresh)}">\n'
        "</head><body></body></html>\n"
    )


def _card_response(body: str) -> Response:
    return Response(
        content=body, media_type="text/html", headers={"Cache-Control": _CACHE_CONTROL},
    )


@router.get("/api/public/documents/{document_id}/preview")
async def public_preview(document_id: str, request: Request) -> Response:
    """Open Graph card for /docs/<id>: always HTTP 200, published or not."""
    origin = _origin(request)
    try:
        await _require_doc(document_id)
        title, description = await _title_and_excerpt(document_id)
        og_type = "article"
    except HTTPException as e:
        if e.status_code != 404:
            raise
        # INVARIANT(security): generic card — nothing derived from the document.
        # Why: anonymous surface; echoing the caller's own input reveals nothing.
        title, description, og_type = _SITE_NAME, _GENERIC_DESCRIPTION, "website"
    return _card_response(_preview_html(
        document_id=document_id, origin=origin, title=title,
        description=description, og_type=og_type,
    ))
