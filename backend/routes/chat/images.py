"""Chat image attach press: one version, shown and sent."""
# SYSTEM: chat-image-press — attach-time image press for chat (composer, row and model share the output)

import asyncio
import base64
import logging

import settings
from fastapi import Depends, File, HTTPException, UploadFile
from thumbnails import press_bytes

from auth import get_current_user
from routes.chat._router import router

logger = logging.getLogger(__name__)

_READ_CHUNK = 1024 * 1024


async def _read_capped(file: UploadFile, max_mb: int) -> bytes:
    """Read the upload in chunks, refusing past the cap mid-stream with 413.

    WHY chunked: an oversized upload is refused as soon as the cap is crossed,
    not after buffering the whole payload.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"Image exceeds {max_mb}MB limit")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/images/normalize")
async def chat_image_normalize(
    file: UploadFile = File(...), user: dict = Depends(get_current_user)
):
    """Press an attached chat image ONCE at attach time: the composer shows,
    the row stores and the model receives exactly these bytes (press_bytes —
    the same press the turn path runs, so nothing re-encodes later).

    # INVARIANT(persisted): the row keeps ONLY the pressed version — the
    # lightbox and the chat-to-document import get it too, and the original
    # is not kept. Why: the person sees exactly the quality the model gets,
    # otherwise they believe small text is readable when it is not.

    # INVARIANT: the press runs in asyncio.to_thread, never inline. Why: it is
    # a full Pillow decode→LANCZOS→encode (2.7s at 4000×3000) — inlining it
    # froze WS collab and every other HTTP request for that whole time (see
    # the same pin on _multimodal_parts in completions_turn.py).
    """
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=422, detail="Unsupported image format")
    max_mb = await settings.get("MAX_IMAGE_SIZE_MB")
    raw = await _read_capped(file, max_mb)
    # Settings resolve BEFORE the thread hop — the thread has no loop to
    # await on (SYSTEM: instance-settings).
    caps = await settings.get_all(["MODEL_IMAGE_MAX_PIXELS", "MODEL_IMAGE_JPEG_QUALITY"])
    try:
        data, out_mime, w, h, source_w, source_h = await asyncio.to_thread(
            press_bytes, raw, file.content_type,
            max_pixels=caps["MODEL_IMAGE_MAX_PIXELS"],
            jpeg_quality=caps["MODEL_IMAGE_JPEG_QUALITY"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat image press failed (%s, %d bytes): %s",
                       file.content_type, len(raw), exc)
        raise HTTPException(
            status_code=422, detail="Image could not be decoded"
        ) from exc
    return {
        "data_url": f"data:{out_mime};base64,{base64.b64encode(data).decode()}",
        "width": w,
        "height": h,
        "source_width": source_w,
        "source_height": source_h,
    }
