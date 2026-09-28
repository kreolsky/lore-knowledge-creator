"""Shared DOCX/PDF→Markdown converter client.

The converter service (Pandoc for .docx, pymupdf4llm for .pdf — both swappable)
is stateless: bytes in → Markdown out. It owns no DB state. Both the worker
(`jobs.tasks._post_to_converter`) and the synchronous web route
(`routes.files.extract_text_for_insert`) call ONE implementation here so the two
callers never diverge.

# SYSTEM: docx_convert — DOCX→Markdown via the stateless converter service
# ARCH: The converter call is HTTP and stateless; safe to call from both the
# web process (short timeout, interactive insert) and the worker (long timeout,
# batch reference conversion). The timeout is the caller's concern, not the
# converter's — pass it explicitly so neither caller is pinned to the other's
# latency budget.
"""

from __future__ import annotations

import asyncio

import http_clients
import httpx
import settings

# Default timeout for the worker (batch reference conversion tolerates long runs).
WORKER_CONVERTER_TIMEOUT = 600
# Default timeout for the synchronous web route (interactive drop-insert; the
# web process must not hang behind a wedged converter).
WEB_CONVERTER_TIMEOUT = 60

_DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_PDF_CONTENT_TYPE = "application/pdf"

# The converter's /convert branches on the FILE EXTENSION (.docx → pandoc,
# .pdf → pymupdf4llm); the multipart content type only has to agree with it.
# WHY a map, not a rename: `post_docx_to_converter` keeps its name (call sites in
# routes/files.py + jobs/tasks/media.py patch or import it by that name).
_CONVERTER_CONTENT_TYPES: dict[str, str] = {
    ".docx": _DOCX_CONTENT_TYPE,
    ".pdf": _PDF_CONTENT_TYPE,
}


def _content_type_for(filename: str) -> str:
    from pathlib import PurePosixPath

    ext = PurePosixPath(filename).suffix.lower()
    return _CONVERTER_CONTENT_TYPES.get(ext, _DOCX_CONTENT_TYPE)

# The WEB process reaches the converter through the shared pool ("docx",
# SYSTEM: http-clients) built with the web timeout below — reused across
# requests (connection pool / keep-alive survives instead of a new client per
# insert). The worker passes its own long-lived client ("media"); both are
# closed at shutdown through the pool (main.py / jobs/worker.py).


async def post_docx_to_converter(
    data: bytes,
    filename: str,
    *,
    client=None,
    timeout: float = WORKER_CONVERTER_TIMEOUT,
) -> str:
    """POST a .docx/.pdf to the stateless converter service, return the Markdown body.

    The converter branches on `filename`'s extension (.docx → pandoc, .pdf →
    pymupdf4llm); the multipart content type is derived from the same extension.

    Raises `RuntimeError` on a non-2xx response and `asyncio.TimeoutError` when the
    converter exceeds `timeout` seconds — so callers (worker/web) translate these
    into their domain-appropriate failure (dead-letter / HTTP 502).

    `client`: an `httpx.AsyncClient`. The worker passes its long-lived client
    (`jobs.tasks._post_to_converter` → the "media" pool entry); the web route
    uses the shared pool client ("docx"). If omitted, the shared pool client
    is used — it is NOT closed here (it survives across requests; closed at
    shutdown).
    """
    if client is None:
        client = http_clients.get_http_client("docx", timeout=WEB_CONVERTER_TIMEOUT)

    converter_url = await settings.get("CONVERTER_URL")
    resp = await asyncio.wait_for(
        client.post(
            f"{converter_url}/convert",
            files={"file": (filename, data, _content_type_for(filename))},
        ),
        timeout=timeout,
    )
    if resp.status_code != httpx.codes.OK:
        raise RuntimeError(
            f"Converter returned {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()["markdown"]
