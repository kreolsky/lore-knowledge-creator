"""Reference-file serving chain — the serve helpers (upload + normalization live
in files_util, the resolution chain in files_service).

# ARCH: the serve helpers live here so the authed router (files.py), the anonymous
# router (public_share.py) and widget.py (API-key) reach them through ONE shared
# module; the security chain they call (resolve_reference_file) is service code in
# files_service.py, shared with mcp_gateway and tool_api/sandbox.
#
# WHY: reference binaries/thumbnails are write-once per reference_id (upload
# creates a new id; DELETE only unlinks, never replaces in place). Why: this immutable
# cache lifetime is only valid while that holds. If in-place file replacement is
# ever added, the URL must become versioned in the same change.
"""

import asyncio
import logging
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from files_service import resolve_reference_file
from thumbnails import generate_thumbnail, get_thumb_path, warm_model_variant

logger = logging.getLogger(__name__)

REFERENCE_FILE_CACHE_CONTROL = "private, max-age=31536000, immutable"


# WHY: asyncio holds only a weak reference to a bare create_task result, so a
# fire-and-forget warm can be GC'd before it runs. Mirrors the _spawn pattern in
# event_bus.py / messages.py — strong ref held here, dropped on completion.
_warm_tasks: set[asyncio.Task] = set()


def _spawn_warm(abs_path: Path, project_id: str, reference_id: str, mime: str) -> None:
    """Schedule the model-variant pre-warm off the response path (never awaited)."""
    task = asyncio.create_task(
        asyncio.to_thread(warm_model_variant, abs_path, project_id, reference_id, mime)
    )
    _warm_tasks.add(task)
    task.add_done_callback(_warm_tasks.discard)


# INVARIANT(anonymous-surface): consumed by routes/public_share.py (the anonymous
# public-share router). Any bytes/headers this returns are served to UNAUTHENTICATED
# readers — a change here is a change to the public-share contract. Why: the
# public-share data-leak class.
async def _serve_thumbnail(ref: dict, reference_id: str) -> FileResponse:
    """Shared serve-or-generate body for image-ref thumbnails — the authed
    `/api/files/{reference_id}/thumb` route and the anonymous
    `/api/public/{token}/files/{reference_id}/thumb` route both resolve here.

    Caller is responsible for the access check (authed: require_document_read;
    public: resolve_share + owning-doc ∈ doc_ids). INVARIANT (write-once binaries ⇒
    immutable cache lifetime; see REFERENCE_FILE_CACHE_CONTROL) holds for both surfaces.

    Security: file resolution + the traversal/exists guard are delegated to
    `resolve_reference_file` (the single home for the chain — plan
    tool-surface-consolidation Step 4), which returns a uniform 404 on any
    failure (missing file / no-file-attached / traversal) so anonymous callers
    get no existence oracle. Same posture as `_serve_reference_file`.

    `reference_id` is passed explicitly (rather than inferred from `ref["id"]`)
    because the row dict may carry SurrealDB's RecordID shape that varies by
    driver; the URL parameter is the unambiguous source of truth.
    """
    if ref.get("media_type") != "image":
        raise HTTPException(status_code=400, detail="Thumbnails only available for image references")

    project_id = ref.get("project_id", "")
    thumb_path = get_thumb_path(project_id, reference_id)

    if thumb_path.is_file():
        return FileResponse(thumb_path, media_type="image/webp", headers={"Cache-Control": REFERENCE_FILE_CACHE_CONTROL})

    # Resolve the source file through the shared security chain (ONE home — Step 4).
    abs_path, _mime, _safe = await resolve_reference_file(ref=ref)

    try:
        await asyncio.to_thread(generate_thumbnail, abs_path, thumb_path)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to generate thumbnail")

    # Pre-warm the model-bound variant beside the thumbnail. plan: model-bound-image-downscale.
    # INVARIANT: fire-and-forget — the thumbnail response must NEVER wait on it.
    # Why: rendering the variant is a full Pillow decode→LANCZOS→encode (measured
    # 2.7s at 4000x3000). Awaited here it delayed every first-time thumbnail by
    # that much, so opening a reference gallery paid it once per image. The warm
    # is pure optimization — chat context renders lazily on miss — so a dropped or
    # still-running warm costs nothing but a slower first chat.
    _spawn_warm(abs_path, project_id, reference_id, _mime)

    return FileResponse(thumb_path, media_type="image/webp", headers={"Cache-Control": REFERENCE_FILE_CACHE_CONTROL})


# INVARIANT(anonymous-surface): consumed by routes/public_share.py — any bytes/headers
# returned here are served to UNAUTHENTICATED readers. Why: the public-share leak class.
async def _serve_reference_file(ref: dict, filename: str) -> FileResponse:
    """Shared serve-body for reference binaries — the authed `/api/files/...` route
    and the anonymous `/api/public/{token}/files/...` route both resolve here.

    Caller is responsible for the access check (authed: require_document_read;
    public: resolve_share + doc ∈ doc_ids). INVARIANT (write-once binaries ⇒
    immutable cache lifetime; see REFERENCE_FILE_CACHE_CONTROL) holds for both surfaces.

    File resolution + the traversal/exists guard are delegated to
    resolve_reference_file (the single home for the security chain — Step 4).
    """
    abs_path, mime, _safe = await resolve_reference_file(
        ref=ref,
    )
    # WHY inline for PDF only: a PDF is the one reference binary the browser can
    # RENDER, and the reference card opens it in a new tab — `attachment` there
    # would turn that action into a download. Every other binary keeps the
    # attachment default; downloads are unaffected either way because the
    # frontend download link carries the `download` attribute, which wins over
    # inline for a same-origin URL (plan pdf-import-pymupdf4llm).
    return FileResponse(
        abs_path, media_type=mime, filename=filename,
        content_disposition_type="inline" if mime == "application/pdf" else "attachment",
        headers={"Cache-Control": REFERENCE_FILE_CACHE_CONTROL},
    )
