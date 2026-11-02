"""Artifact serve — the cookieless-origin HTTP surface.

# Routes half of the html-artifacts seam (see SYSTEM: artifact-serve): the
# host-split middleware (one process, two hosts), /a/<id> (a page behind a
# short-lived signed token), /lib/* (the pinned self-hosted libraries), and
# the two artifact-url mint routes. The config/manifest/token CORE lives in
# artifacts.py (the module non-route layers import — never this one).
#
# # INVARIANT(security): the artifact host never sets and never reads a
# # cookie. Why: it exists to isolate untrusted page source from the app's
# # session; any credential leg on that host re-couples them. No handler on
# # /a/* or /lib/* takes an auth dependency or touches lore_session, and the
# # mint routes are APP-host routes.
"""
from __future__ import annotations

import logging
import mimetypes
import os

import artifacts
from artifacts import (
    ARTIFACT_LIB_ROOT,
    ARTIFACT_URL_TTL_S,
    artifact_enabled,
    check_artifact_origin_config,
    is_artifact_path,
    mint_artifact_url,
    on_artifact_host,
    verify_artifact_token,
)
from collab.events import merge_live_content
from fastapi import APIRouter, Depends, HTTPException
from security_log import log_security
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from access import require_document_read
from auth import get_current_user
from db import fetch_one
from routes.public_share import _require_doc

__all__ = [
    "router",
    "ArtifactHostMiddleware",
    "check_artifact_origin_config",
]

router = APIRouter()
logger = logging.getLogger(__name__)

_FORBIDDEN_PAGE = (
    "<!doctype html><meta charset=\"utf-8\"><title>Unavailable</title>"
    "<style>body{font-family:system-ui,sans-serif;color:#888;padding:2rem}"
    "</style><p>This page is unavailable or its link has expired.</p>"
)


# ─── Host-split middleware ───────────────────────────────────────────────────


class ArtifactHostMiddleware:
    """Split the two hosts served by this one process.

    On the artifact host, ONLY /a/* and /lib/* exist — every other path 404s
    (the API, the WS channels and every authed surface are unreachable
    there). On any other host, /a/* and /lib/* 404 (an artifact URL pasted
    onto the app host serves nothing; the token never reaches a handler).
    With hosting unset, /a/* and /lib/* 404 everywhere (a valid deploy).
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            host = ""
            for key, value in scope.get("headers", []):
                if key == b"host":
                    host = value.decode("latin-1")
                    break
            on_artifact = on_artifact_host(host)
            if on_artifact and not is_artifact_path(path):
                logger.warning("artifact host refused non-artifact path %s", path)
                await _refuse(scope, send)
                return
            if not on_artifact and is_artifact_path(path):
                await _refuse(scope, send)
                return
        await self.app(scope, receive, send)


async def _refuse(scope, send) -> None:
    # WHY: a websocket scope only accepts websocket.* messages — closing before
    # accept is the ASGI way to refuse the handshake (the server answers 403).
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = b"Not Found"
    await send({
        "type": "http.response.start",
        "status": 404,
        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii"))],
    })
    await send({"type": "http.response.body", "body": body})


# ─── Response headers ─────────────────────────────────────────────────────────


def _cors_origins() -> list[str]:
    """The CORS origin list, resolved exactly as main.py resolves it.

    frame-ancestors must name every origin a browser reaches the app from,
    or the embedded frame blanks — the same env var, the same dev fallback
    (imported lazily from its one owner; no second copy of the constant).
    """
    raw = os.environ.get("CORS_ORIGINS", "")
    if not raw:
        from main import _DEFAULT_CORS_ORIGINS

        raw = _DEFAULT_CORS_ORIGINS
    return [o.strip() for o in raw.split(",") if o.strip()]


def _artifact_response_headers(cors_origins: list[str]) -> dict[str, str]:
    """The /a/* response headers. No third-party host appears anywhere.

    Why `sandbox` in the header (not only the iframe attribute): the
    attribute does not apply when /a/<id> is opened top-level from a copied
    URL — the header sandboxes the page everywhere. `connect-src 'none'`
    shuts the v1 page off the network entirely; libraries load from this
    same origin's /lib/ only.
    """
    lib = f"{artifacts.artifact_origin()}/lib/"
    csp = (
        "sandbox allow-scripts; "
        "default-src 'none'; "
        f"script-src 'unsafe-inline' {lib}; "
        f"style-src 'unsafe-inline' {lib}; "
        "img-src data: blob:; "
        f"font-src data: {lib}; "
        "connect-src 'none'; "
        "form-action 'none'; "
        "base-uri 'none'; "
        f"frame-ancestors {' '.join(cors_origins)};"
    )
    return {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "private, no-store",
    }


def _forbidden_response(cors_origins: list[str]) -> Response:
    return Response(
        content=_FORBIDDEN_PAGE,
        status_code=403,
        media_type="text/html; charset=utf-8",
        headers=_artifact_response_headers(cors_origins),
    )


# ─── /a/<id> — serve one artifact page ────────────────────────────────────────


async def _serve_artifact(document_id: str, request: Request) -> Response:
    cors_origins = _cors_origins()

    token = request.query_params.get("t", "")
    if not verify_artifact_token(document_id, token):
        log_security("artifact_token_rejected", document_id=document_id)
        return _forbidden_response(cors_origins)
    row = await fetch_one("documents", document_id)
    if (not row or row.get("deleted_at")
            or (row.get("media_type") or "") != "html"):
        log_security("artifact_row_refused", document_id=document_id)
        return _forbidden_response(cors_origins)
    # Live collab content wins over the flushed row — an unflushed edit in the
    # Source toggle still serves (the editor's own merge rule).
    content = merge_live_content(row, document_id).get("content") or ""
    return Response(
        content=content,
        media_type="text/html; charset=utf-8",
        headers=_artifact_response_headers(cors_origins),
    )


# ─── /lib/* — pinned self-hosted libraries ────────────────────────────────────


async def _serve_lib(name_version: str, file_path: str) -> Response:
    # `<name>@<version>` and the file must BOTH be present in the manifest —
    # the filesystem is reachable only through the pinned list, so no path
    # traversal reaches it (defense in depth: the resolved path is also
    # containment-checked against ARTIFACT_LIB_ROOT).
    entry = None
    for lib in artifacts._manifest().get("libs", []):
        if f"{lib['name']}@{lib['version']}" == name_version:
            entry = lib
            break
    if entry is None or file_path not in {f["path"] for f in entry.get("files", [])}:
        raise HTTPException(status_code=404, detail="Not Found")
    root = (ARTIFACT_LIB_ROOT / name_version).resolve()
    target = (root / file_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    media_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    return FileResponse(
        target,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# ─── URL minting routes (app host) ────────────────────────────────────────────


async def _artifact_url_for(document_id: str) -> dict:
    """Shared mint body: row checks + the signed URL. 404/422 vocabulary:

    404 missing/deleted (uniform), 422 non-artifact row, 422 hosting off.
    """
    if not artifact_enabled():
        raise HTTPException(
            status_code=422, detail="artifact hosting is not configured",
        )
    row = await fetch_one("documents", document_id)
    if not row or row.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Document not found")
    if (row.get("media_type") or "") != "html":
        raise HTTPException(
            status_code=422, detail="Document is not an html artifact",
        )
    return {"url": mint_artifact_url(document_id), "expires_in": ARTIFACT_URL_TTL_S}


@router.get("/api/documents/{document_id}/artifact-url")
async def document_artifact_url(
    document_id: str, user: dict = Depends(get_current_user),
):
    """Signed short-lived artifact URL (any project read access)."""
    await require_document_read(document_id, user)
    return await _artifact_url_for(document_id)


@router.get("/api/public/documents/{document_id}/artifact-url")
async def public_document_artifact_url(document_id: str):
    """Signed short-lived artifact URL for a guest, through the ONLY public
    funnel (`_require_doc` — resolve_share + membership; a doc outside any
    live share 404s here exactly like every other public read)."""
    await _require_doc(document_id)
    return await _artifact_url_for(document_id)


# ─── Artifact-host routes (the middleware is what makes them reachable only
# on the artifact origin) ──────────────────────────────────────────────────────


@router.get("/a/{document_id}")
async def serve_artifact(document_id: str, request: Request):
    return await _serve_artifact(document_id, request)


@router.get("/lib/{name_version}/{file_path:path}")
async def serve_lib(name_version: str, file_path: str):
    return await _serve_lib(name_version, file_path)
