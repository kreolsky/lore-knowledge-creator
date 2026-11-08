"""Tests for model-bound image normalization (SYSTEM: thumbnails + chat-context).

Covers the decisions in .kilo/plans/model-bound-image-downscale.md:
- Area-based pixel cap (1.25 Mpx), no upscale, aspect preserved.
- Format: PNG stays PNG; JPEG/GIF/WebP -> JPEG q85; WebP never reaches the model.
- EXIF transpose + alpha->RGB; byte-stable output; corrupt -> original (non-fatal).
- Non-vision model: image parts stripped AND a warning appended (no silent drop).
"""
import io
from uuid import uuid4

from PIL import Image


def _patch_vision(monkeypatch, ok: bool) -> None:
    """Pin the vision gate on driver.client — build_context imports
    `agent_capability` from THERE lazily (cycle break), so that is the only
    namespace the call sees.

    INVARIANT: the gate is pinned, never inherited from the runner. Why: the real
    gate asks the DRIVER (which resolves the model off the gateway roster), and
    the model falls back to config.CHAT_MODEL — set to a vision model on
    dev/gray, EMPTY on CI (.env.example ships it commented), so an unpinned test
    passes locally and fails on CI as a phantom code defect.
    """
    import driver.client

    async def _fake(_model=None):
        return {"available": True, "vision": ok}

    monkeypatch.setattr(driver.client, "agent_capability", _fake)


def _png_bytes(w: int, h: int, color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color=color).save(buf, "PNG")
    return buf.getvalue()


def _save_bytes(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


# ─── downscale_bytes: format rules ────────────────────────────────────────────


def test_no_path_emits_webp():
    """INVARIANT: no input format yields a WebP output (silently dropped locally)."""
    from thumbnails import downscale_bytes

    from config import MODEL_IMAGE_SAFE_MIMES

    assert "image/webp" not in MODEL_IMAGE_SAFE_MIMES
    for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"),
                      ("WEBP", "image/webp"), ("GIF", "image/gif")):
        raw = _save_bytes(Image.new("RGB", (32, 32), (1, 2, 3)), fmt)
        _out, out_mime = downscale_bytes(raw, mime)
        assert out_mime in ("image/png", "image/jpeg"), (fmt, out_mime)


# ─── downscale_bytes: normalize (EXIF / alpha) + determinism + robustness ─────


def test_same_input_twice_byte_identical():
    from thumbnails import downscale_bytes

    src = _png_bytes(2000, 1500, color=(12, 34, 56))
    a, _ = downscale_bytes(src, "image/png")
    b, _ = downscale_bytes(src, "image/png")
    assert a == b


# ─── press_bytes: the cross-process contract with dsh ────────────────────────


def _icc_bytes() -> bytes:
    from PIL import ImageCms

    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _jpeg_exif_orientation_bytes() -> bytes:
    from PIL import ExifTags

    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), (10, 20, 30)).save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def _png16_rgb_bytes(w: int = 4, h: int = 2) -> bytes:
    """A 16-bit-per-sample truecolor PNG (bit depth 16, color type 2) built
    raw: Pillow cannot WRITE this shape, but it is exactly the source Pillow
    reports as plain mode RGB with no metadata — the reason a PNG never
    passes through the press unproven."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data)))

    ihdr = struct.pack(">IIBBBBB", w, h, 16, 2, 0, 0, 0)
    row = b"\x00" + b"\xff\xff\x00\x00\x00\x00" * w  # filter 0 + w red 16-bit pixels
    raw = row * h
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _apng_bytes() -> bytes:
    buf = io.BytesIO()
    first = Image.new("RGB", (8, 8), (200, 0, 0))
    second = Image.new("RGB", (8, 8), (0, 0, 200))
    first.save(buf, "PNG", save_all=True, append_images=[second])
    return buf.getvalue()


def _press_sources() -> list[tuple[str, str, bytes]]:
    png_icc = io.BytesIO()
    Image.new("RGB", (30, 20), (1, 2, 3)).save(png_icc, "PNG", icc_profile=_icc_bytes())
    jpeg_icc = io.BytesIO()
    Image.new("RGB", (30, 20), (4, 5, 6)).save(jpeg_icc, "JPEG", icc_profile=_icc_bytes())
    return [
        ("PNG with ICC", "image/png", png_icc.getvalue()),
        ("JPEG with ICC", "image/jpeg", jpeg_icc.getvalue()),
        ("JPEG with EXIF orientation", "image/jpeg", _jpeg_exif_orientation_bytes()),
        ("16-bit RGB PNG", "image/png", _png16_rgb_bytes()),
        ("APNG", "image/png", _apng_bytes()),
    ]


# TEST-WHY: cross-process-contract — dsh passes stored chat image bytes to the model unchanged only when they are single-frame 8-bit sRGB metadata-free, so over every violating source kind (ICC, EXIF orientation, 16-bit depth, animation) the press output must come out clean AND byte-identical on a second press, or a later press (a resend, a fork) would hand dsh different bytes than the row stored.
def test_press_output_is_clean_and_idempotent():
    from thumbnails import press_bytes

    for label, mime, raw in _press_sources():
        data, out_mime, w, h, sw, sh = press_bytes(
            raw, mime, max_pixels=1_250_000, jpeg_quality=85
        )
        assert out_mime == ("image/png" if mime == "image/png" else "image/jpeg"), label
        assert w * h <= 1_250_000 and w * h <= sw * sh, f"{label}: upscaled or over cap"
        out = Image.open(io.BytesIO(data))
        assert not out.info.get("icc_profile"), f"{label}: ICC survived the press"
        assert not out.info.get("exif"), f"{label}: exif survived the press"
        assert out.format in ("PNG", "JPEG") and not getattr(out, "is_animated", False), label
        again, again_mime, *_ = press_bytes(
            data, out_mime, max_pixels=1_250_000, jpeg_quality=85
        )
        assert again == data, f"{label}: second press is not byte-identical"
        assert again_mime == out_mime, label


# ─── context: vision gate ─────────────────────────────────────────────────────


def _ctx_image_ref(monkeypatch, *, ref_id, content="a map", mime="image/png"):
    from routes.chat import context as ctx_module

    from config import STORAGE_PATH

    # The on-disk bytes must really be in `mime`'s format — a WebP reference whose
    # file is secretly a PNG would not exercise the transcode path at all.
    # TEST-WHY: seeded under the REAL storage — context resolves image refs via
    # files_util.resolve_stored_file (rot-epic step 2); routes.chat.context has
    # no STORAGE_PATH attribute to patch anymore (stale patch target).
    fmt = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[mime]
    rel = f"model-variant-ctx/{uuid4().hex[:8]}/{ref_id}.png"
    img = STORAGE_PATH / rel
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(_save_bytes(Image.new("RGB", (900, 700), (7, 8, 9)), fmt))

    async def fake_fetch_one(table, rid):
        if table == "documents" and rid == ref_id:
            return {"id": f"documents:{rid}", "deleted_at": None,
                    "project_id": "proj-1", "content": content, "title": "T",
                    "is_reference": True, "media_type": "image",
                    "file_path": rel, "file_meta": {"mime_type": mime}}
        return None

    async def fake_get_db():
        return None

    async def fake_build_ref_map(_db, *_ids):
        return {ref_id: {"is_reference": True}}

    monkeypatch.setattr(ctx_module, "get_db", fake_get_db)
    monkeypatch.setattr(ctx_module, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(ctx_module, "build_ref_map", fake_build_ref_map)


async def test_non_vision_model_strips_image_and_warns(monkeypatch, tmp_path):
    from routes.chat import context as ctx_module

    from models import CompletionRequest

    _ctx_image_ref(monkeypatch, ref_id="ref-strip")
    _patch_vision(monkeypatch, False)

    body = CompletionRequest(
        user_msg_id=uuid4(),
        messages=[{"role": "user", "content": "hi"}],
        context_ids=["ref-strip"], model="deepseek/pro",
    )
    result = await ctx_module.build_context(body, "proj-1", {"document_id": None})

    assert not any(p.get("type") == "image_url" for p in result.image_parts), \
        result.image_parts
    assert any(w.get("code") == "image_not_delivered" for w in result.warnings), \
        result.warnings
