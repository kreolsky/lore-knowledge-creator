"""Thumbnail generation for image references + the model-bound image press.

- `_thumb.webp`: center-cropped square WebP for the UI card (size wins over fidelity).
- `press_bytes` / `downscale_bytes`: pixel-capped, format-safe raster for LLM
  delivery. WebP is FORBIDDEN there (silently dropped by the local models) — see
  config.MODEL_IMAGE_SAFE_MIMES.
"""
# SYSTEM: thumbnails — image thumbnail generation and serving + the model-bound image press

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


# Source metadata the press never lets through — neither into a re-encode nor
# through a pass-through. Mirrors what dsh counts as retained metadata
# (attachment-local image.ts carriesRetainedMetadata): exif, ICC, XMP, IPTC,
# Photoshop markers, comments. Anything carrying one of these re-encodes.
_RETAINED_METADATA_KEYS = ("exif", "icc_profile", "xmp", "comment", "iptc", "photoshop")


def _decode_flattened(raw: bytes) -> tuple[Image.Image, int, int, str, bool]:
    """Decode a source into an oriented, flattened RGB image.

    Returns (img, source_w, source_h, source_mode, dirty). `source_*` describe
    the decoded source BEFORE any conversion or orientation (the stored pixel
    dims); `dirty` marks a source that could not have arrived clean —
    transparency or retained metadata — and the JPEG pass-through in
    press_bytes refuses a dirty source.

    INVARIANT: alpha is composited on WHITE, never dropped. Why: a plain
    convert("RGB") discards the alpha channel and renders a transparent
    background black; has_transparency_data is the test, NOT the mode,
    because an RGB PNG can still carry a tRNS chunk.
    """
    img = Image.open(io.BytesIO(raw))
    img.load()  # decode up front: a bomb or corrupt stream raises before any work
    source_w, source_h = img.size
    source_mode = img.mode
    # Read dirtiness BEFORE exif_transpose: an orientation tag is exif, and a
    # transposed image must not pass through as "clean" (its pixels changed).
    dirty = bool(img.has_transparency_data) or any(
        img.info.get(k) for k in _RETAINED_METADATA_KEYS
    )
    img = ImageOps.exif_transpose(img)
    if img.has_transparency_data:
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        img = Image.alpha_composite(
            Image.new("RGBA", img.size, (255, 255, 255, 255)), img
        ).convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return img, source_w, source_h, source_mode, dirty


def press_bytes(
    raw: bytes, mime: str, *, max_pixels: int, jpeg_quality: int,
) -> tuple[bytes, str, int, int, int, int]:
    """STRICT image press: decode → orient → white-composite → cap → re-encode.

    Raises on any failure (unlike downscale_bytes, which falls back to the
    source bytes): the attach route turns an exception into a 422, because at
    attach time there is no honest fallback — the person must see exactly the
    bytes the model gets, and passing unpressable bytes through would show one
    thing and send another.

    Returns (data, out_mime, width, height, source_width, source_height).

    INVARIANT: the press is IDEMPOTENT — pressing its own output returns
    byte-identical bytes. Why: cross-process contract — dsh stores the bytes
    and passes them to the model request unchanged only when they are
    single-frame, 8-bit sRGB and metadata-free; our output is provably that
    shape, so what the row stores is what the model receives.

    ARCH: a JPEG already within the pixel cap, in RGB and free of metadata
    returns UNTOUCHED — re-encoding a JPEG loses a generation, and a clean
    JPEG has nothing to press. A PNG is ALWAYS re-saved: lossless and
    deterministic, and it must not pass through because Pillow reports a
    16-bit RGB PNG and an APNG as plain mode RGB with no metadata (observed
    on gray) — a PNG's cleanliness cannot be proven by inspection, only by
    re-encoding it.
    """
    img, source_w, source_h, source_mode, dirty = _decode_flattened(raw)
    w, h = img.size
    resized = w * h > max_pixels
    if resized:
        scale = (max_pixels / (w * h)) ** 0.5
        img = img.resize(
            (max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS
        )
        w, h = img.size
    out_mime = _out_mime_for(mime)
    if mime == "image/jpeg" and source_mode == "RGB" and not dirty and not resized:
        return raw, out_mime, w, h, source_w, source_h
    # WHY: every re-encode clears img.info on BOTH branches — the output must
    # carry no ICC, exif, XMP or comment. dsh counts them as metadata and
    # re-encodes a carrying image at admission, which would break the
    # one-version contract (the model would get bytes the row never stored).
    img.info = {}
    buf = io.BytesIO()
    if out_mime == "image/png":
        img.save(buf, "PNG")
    else:
        img.save(buf, "JPEG", quality=jpeg_quality)
    return buf.getvalue(), out_mime, w, h, source_w, source_h


def downscale_bytes(
    raw: bytes, mime: str, *, max_pixels: int | None = None, jpeg_quality: int | None = None,
) -> tuple[bytes, str] | None:
    """Normalize image bytes for model delivery. Returns (bytes, out_mime) | None.

    The strict press (press_bytes) with a non-fatal fallback around it: caps
    total pixels to `max_pixels` (area-based, no upscale), applies EXIF
    orientation, composites alpha on white, strips metadata, and rewrites to
    the safe set {PNG, JPEG}: PNG→PNG, everything else→JPEG. Idempotent and
    deterministic (byte-stable across runs).

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
    try:
        data, out_mime, *_ = press_bytes(
            raw, mime, max_pixels=max_pixels, jpeg_quality=jpeg_quality
        )
        return data, out_mime
    except Exception as exc:  # noqa: BLE001
        logger.warning("downscale_bytes failed (%s): %s — fallback", mime, exc)
        if mime == "image/webp":
            return None  # cannot safely send the original WebP
        return raw, mime
