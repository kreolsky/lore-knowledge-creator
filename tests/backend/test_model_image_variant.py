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


# ─── context: vision gate ─────────────────────────────────────────────────────


def _ctx_image_ref(monkeypatch, tmp_path, *, ref_id, content="a map", mime="image/png"):
    from routes.chat import context as ctx_module

    # The on-disk bytes must really be in `mime`'s format — a WebP reference whose
    # file is secretly a PNG would not exercise the transcode path at all.
    fmt = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[mime]
    img = tmp_path / f"{ref_id}.png"
    img.write_bytes(_save_bytes(Image.new("RGB", (900, 700), (7, 8, 9)), fmt))
    monkeypatch.setattr(ctx_module, "STORAGE_PATH", tmp_path)

    async def fake_fetch_one(table, rid):
        if table == "documents" and rid == ref_id:
            return {"id": f"documents:{rid}", "deleted_at": None,
                    "project_id": "proj-1", "content": content, "title": "T",
                    "is_reference": True, "media_type": "image",
                    "file_path": f"{ref_id}.png", "file_meta": {"mime_type": mime}}
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

    _ctx_image_ref(monkeypatch, tmp_path, ref_id="ref-strip")
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
