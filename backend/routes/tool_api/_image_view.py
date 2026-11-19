"""Tool-API image views — the ONE tail every view tool serves its bytes through.

sniff → press (pixel cap, same settings as chat attach) → base64 envelope.
Leaf: imports only thumbnails, settings and fastapi — never a domain module.
"""
import asyncio
import base64
import logging

import settings
from fastapi import HTTPException
from thumbnails import press_bytes

logger = logging.getLogger(__name__)

# The per-image view cap, in DECODED bytes — the same figure the driver's image
# admission uses (dsh DEFAULT_MAX_IMAGE_BYTES, the min read_image honours), so
# what this route serves always fits what saveImage will accept.
VIEW_CHAT_IMAGE_MAX_BYTES = 20 * 1024 * 1024


def sniff_media_type(data: bytes) -> str | None:
    """The image type from its magic bytes — the extension is never trusted."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def image_envelope(raw: bytes, *, unsupported_detail: str) -> dict:
    """`{image: {media_type, data}, width, height}` of `raw`, pressed to the model cap.

    415 with `unsupported_detail` when the bytes are not png/jpeg/gif/webp;
    415 "image could not be decoded" when the press fails.
    """
    mime = sniff_media_type(raw)
    if mime is None:
        raise HTTPException(status_code=415, detail=unsupported_detail)
    # WHY: settings resolve BEFORE the thread hop — the thread has no loop to await on.
    caps = await settings.get_all(["MODEL_IMAGE_MAX_PIXELS", "MODEL_IMAGE_JPEG_QUALITY"])
    try:
        # WHY: the press runs in asyncio.to_thread, never inline.
        # Why: a full Pillow decode→resize→encode blocks the event loop for seconds.
        data, out_mime, w, h, _sw, _sh = await asyncio.to_thread(
            press_bytes, raw, mime,
            max_pixels=caps["MODEL_IMAGE_MAX_PIXELS"],
            jpeg_quality=caps["MODEL_IMAGE_JPEG_QUALITY"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("image view press failed (%s, %d bytes): %s", mime, len(raw), exc)
        raise HTTPException(status_code=415, detail="image could not be decoded") from exc
    return {
        "image": {"media_type": out_mime, "data": base64.b64encode(data).decode("ascii")},
        "width": w, "height": h,
    }
