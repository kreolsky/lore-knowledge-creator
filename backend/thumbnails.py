"""Thumbnail + model-variant generation for image references.

Two derivatives per image reference, deliberately different:
- `_thumb.webp`: center-cropped square WebP for the UI card (size wins over fidelity).
- `_model.{png|jpg}`: pixel-capped, format-safe raster for LLM delivery. WebP is
  FORBIDDEN here (silently dropped by the local models) — see config.MODEL_IMAGE_SAFE_MIMES.
"""
# SYSTEM: thumbnails — image thumbnail + model-variant generation and serving

import io
import logging
from pathlib import Path

import settings
from PIL import Image, ImageOps

from config import (
    STORAGE_PATH,
)

logger = logging.getLogger(__name__)

THUMB_SIZE = 200
THUMB_FILENAME = "_thumb.webp"
MODEL_VARIANT_FILENAME = "_model"  # extension chosen per output mime (png|jpg)


def generate_thumbnail(src_path: Path, dst_path: Path, size: int = THUMB_SIZE) -> tuple[int, int]:
    """Generate center-cropped square WebP thumbnail. Returns (orig_w, orig_h).

    # ARCH: No upscale — if original is smaller than `size` on either side,
    # the image is center-cropped to square at its original resolution.
    """
    img = Image.open(src_path)
    orig_w, orig_h = img.size

    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    min_side = min(orig_w, orig_h)
    left = (orig_w - min_side) // 2
    top = (orig_h - min_side) // 2
    img = img.crop((left, top, left + min_side, top + min_side))

    if min_side > size:
        img = img.resize((size, size), Image.LANCZOS)

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst_path, "WEBP", quality=80)

    return orig_w, orig_h


def get_thumb_path(project_id: str, ref_id: str) -> Path:
    """Return absolute path to thumbnail file."""
    return STORAGE_PATH / project_id / ref_id / THUMB_FILENAME


# ─── Model-bound variant (SYSTEM: thumbnails) ────────────────────────────────


def _out_mime_for(src_mime: str) -> str:
    """PNG stays PNG; everything else (incl. WebP) → JPEG. Never WebP (INVARIANT)."""
    return "image/png" if src_mime == "image/png" else "image/jpeg"


def downscale_bytes(
    raw: bytes, mime: str, *, max_pixels: int | None = None, jpeg_quality: int | None = None,
) -> tuple[bytes, str] | None:
    """Normalize image bytes for model delivery. Returns (bytes, out_mime) | None.

    Caps total pixels to `max_pixels` (area-based, no upscale), applies
    EXIF orientation, converts to RGB (drops alpha), strips metadata, and rewrites
    to the safe set {PNG, JPEG}: PNG→PNG, everything else→JPEG. Deterministic
    (byte-stable across runs).

    `max_pixels`/`jpeg_quality` are CALLER-RESOLVED settings (MODEL_IMAGE_MAX_PIXELS /
    MODEL_IMAGE_JPEG_QUALITY via settings.get): this function is sync by design —
    it runs inside asyncio.to_thread on the chat turn path, where no loop can be
    awaited, so it must not resolve settings itself. None → the config default
    (callers that never left the env world may omit them).

    Failure is non-fatal: a Pillow error on a SAFE source format falls back to the
    original bytes (logged). A WebP that cannot be transcoded returns None — the
    caller must DROP it, because returning the original WebP is the silent-failure
    case this system exists to prevent (config.MODEL_IMAGE_SAFE_MIMES INVARIANT).
    """
    if max_pixels is None:
        max_pixels = settings.bootstrap_value("MODEL_IMAGE_MAX_PIXELS")
    if jpeg_quality is None:
        jpeg_quality = settings.bootstrap_value("MODEL_IMAGE_JPEG_QUALITY")
    out_mime = _out_mime_for(mime)
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if w * h > max_pixels:
            scale = (max_pixels / (w * h)) ** 0.5
            img = img.resize(
                (max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS
            )
        buf = io.BytesIO()
        if out_mime == "image/png":
            img.save(buf, "PNG")
        else:
            img.save(buf, "JPEG", quality=jpeg_quality)
        return buf.getvalue(), out_mime
    except Exception as exc:  # noqa: BLE001
        logger.warning("downscale_bytes failed (%s): %s — fallback", mime, exc)
        if mime == "image/webp":
            return None  # cannot safely send the original WebP
        return raw, mime


def get_model_variant_path(project_id: str, ref_id: str, out_mime: str) -> Path:
    """Return absolute path to the cached model variant (extension from out_mime)."""
    ext = "png" if out_mime == "image/png" else "jpg"
    return STORAGE_PATH / project_id / ref_id / f"{MODEL_VARIANT_FILENAME}.{ext}"


def _mime_from_image(src_path: Path) -> str:
    """Detect source mime via Pillow (for eager sites lacking stored metadata)."""
    try:
        with Image.open(src_path) as im:
            fmt = (im.format or "").upper()
    except Exception:  # noqa: BLE001
        return "image/jpeg"
    return {
        "PNG": "image/png", "JPEG": "image/jpeg",
        "WEBP": "image/webp", "GIF": "image/gif",
    }.get(fmt, "image/jpeg")


def render_model_variant(
    src_path: Path, project_id: str, ref_id: str, src_mime: str,
) -> tuple[bytes, str] | None:
    """Downscale+encode once and cache the variant beside _thumb.webp.

    Returns (bytes, out_mime), or None when the source is WebP and could not be
    transcoded (the caller must drop it). Idempotent + lazy.
    """
    raw = src_path.read_bytes()
    result = downscale_bytes(raw, src_mime)
    if result is None:
        return None
    data, out_mime = result
    dst = get_model_variant_path(project_id, ref_id, out_mime)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    return data, out_mime


def get_or_render_model_variant(
    project_id: str, ref_id: str, src_path: Path, src_mime: str,
) -> tuple[bytes, str] | None:
    """Return the cached variant, rendering it lazily if missing.

    None = WebP that could not be transcoded (caller drops + warns).
    """
    out_mime = _out_mime_for(src_mime)
    cached = get_model_variant_path(project_id, ref_id, out_mime)
    if cached.is_file():
        return cached.read_bytes(), out_mime
    return render_model_variant(src_path, project_id, ref_id, src_mime)


def warm_model_variant(
    src_path: Path, project_id: str, ref_id: str, src_mime: str | None = None,
) -> None:
    """Best-effort eager generation of the model variant (pre-warm beside thumbnail).

    Called beside `generate_thumbnail` at both sites (inline serve + arq job) so the
    variant exists whenever the thumbnail does. Failures are logged and swallowed —
    the lazy read path (chat context) regenerates on demand, so a warm miss is never
    fatal. `src_mime` None → detected from the image (for sites lacking metadata).

    Idempotent: skips when the variant is already cached (the thumbnail-serve path
    is hot — never re-render what is already on disk).
    """
    try:
        mime = src_mime or _mime_from_image(src_path)
        out_mime = _out_mime_for(mime)
        if get_model_variant_path(project_id, ref_id, out_mime).is_file():
            return  # already cached — no work
        render_model_variant(src_path, project_id, ref_id, mime)
    except Exception as exc:  # noqa: BLE001
        logger.warning("warm_model_variant failed for ref %s: %s", ref_id, exc)
