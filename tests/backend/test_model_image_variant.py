"""Tests for model-bound image normalization (SYSTEM: thumbnails + chat-context).

Covers the decisions in .kilo/plans/model-bound-image-downscale.md:
- Area-based pixel cap (1.25 Mpx), no upscale, aspect preserved.
- Format: PNG stays PNG; JPEG/GIF/WebP -> JPEG q85; WebP never reaches the model.
- EXIF transpose + alpha->RGB; byte-stable output; corrupt -> original (non-fatal).
- Non-vision model: image parts stripped AND a warning appended (no silent drop).
"""
import base64
import io

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


def _dims(raw: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(raw)) as im:
        return im.size


def _uri_mime(data_uri: str) -> str:
    """Extract the declared mime from a `data:<mime>;base64,<payload>` URI.

    Parsing beats a startswith() prefix guess: `data:image/webp,` never matches a
    real URI (the `;base64` sits between), so such an assertion cannot fail.
    """
    assert data_uri.startswith("data:"), data_uri
    return data_uri[len("data:"):].split(";", 1)[0]


def _format(raw: bytes) -> str:
    with Image.open(io.BytesIO(raw)) as im:
        return (im.format or "").upper()


# ─── downscale_bytes: pixel cap ───────────────────────────────────────────────


def test_overcap_input_area_within_cap_and_aspect_preserved():
    from thumbnails import downscale_bytes

    from config import MODEL_IMAGE_MAX_PIXELS

    out, _mime = downscale_bytes(_png_bytes(4000, 3000), "image/png")
    w, h = _dims(out)
    assert w * h <= MODEL_IMAGE_MAX_PIXELS, (w, h, w * h)
    # Aspect preserved within a pixel (4000:3000 = 4:3).
    assert abs((w / h) - (4000 / 3000)) < 0.02, (w, h)


def test_extreme_aspect_capped_by_area_not_long_edge():
    """3000x500 must be capped by AREA; a long-edge cap would shrink it wrongly."""
    from thumbnails import downscale_bytes

    from config import MODEL_IMAGE_MAX_PIXELS

    out, _mime = downscale_bytes(_png_bytes(3000, 500), "image/png")
    w, h = _dims(out)
    assert w * h <= MODEL_IMAGE_MAX_PIXELS, (w, h, w * h)
    # Aspect 6:1 preserved.
    assert abs((w / h) - 6.0) < 0.05, (w, h)


def test_undercap_input_dimensions_untouched():
    """Under-cap input is never upscaled."""
    from thumbnails import downscale_bytes

    out, _mime = downscale_bytes(_png_bytes(800, 600), "image/png")
    assert _dims(out) == (800, 600)


# ─── downscale_bytes: format rules ────────────────────────────────────────────


def test_png_in_png_out():
    from thumbnails import downscale_bytes

    out, mime = downscale_bytes(_png_bytes(64, 64), "image/png")
    assert mime == "image/png"
    assert _format(out) == "PNG"


def test_jpeg_gif_webp_in_jpeg_out():
    from thumbnails import downscale_bytes

    src = _png_bytes(64, 64)
    # Re-encode the PNG into each source format.
    jpeg = _save_bytes(Image.open(io.BytesIO(src)), "JPEG")
    gif = _save_bytes(Image.open(io.BytesIO(src)).convert("P"), "GIF")
    webp = _save_bytes(Image.open(io.BytesIO(src)), "WEBP")

    for raw in (jpeg, gif, webp):
        out, mime = downscale_bytes(raw, "image/jpeg" if raw is jpeg else
                                    ("image/gif" if raw is gif else "image/webp"))
        assert mime == "image/jpeg", mime
        assert _format(out) == "JPEG"


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


def test_rgba_converted_to_rgb():
    from thumbnails import downscale_bytes

    rgba = _save_bytes(Image.new("RGBA", (48, 48), (10, 20, 30, 128)), "PNG")
    out, _mime = downscale_bytes(rgba, "image/png")
    with Image.open(io.BytesIO(out)) as im:
        # Unconditional: the code always converts to RGB. Accepting "L" too would
        # make this pass on an outcome the contract does not allow.
        assert im.mode == "RGB", im.mode


def test_exif_orientation_is_applied_to_pixels():
    """EXIF orientation 6 (90° CW) must be baked into the pixels.

    The model sees raw pixels, never the orientation tag — an un-transposed
    upload arrives sideways. Asserted on BOTH axes so the test can actually fail:
    a landscape source must come back portrait, and the marker pixel must land
    where a 90° CW rotation puts it.
    """
    from thumbnails import downscale_bytes

    # 40x20 landscape, white, with a single black marker at top-left.
    img = Image.new("RGB", (40, 20), (255, 255, 255))
    img.putpixel((0, 0), (0, 0, 0))
    exif = Image.Exif()
    exif[274] = 6  # Orientation: rotate 90° CW on display
    tagged = io.BytesIO()
    img.save(tagged, "JPEG", exif=exif, quality=95)

    # Guard: the source really carries the tag (else the test proves nothing).
    with Image.open(io.BytesIO(tagged.getvalue())) as src:
        assert src.getexif().get(274) == 6
        assert src.size == (40, 20)

    out, _mime = downscale_bytes(tagged.getvalue(), "image/jpeg")

    # Landscape -> portrait: dimensions swapped by the transpose.
    assert _dims(out) == (20, 40), _dims(out)
    # Top-left marker rotates to the top-right corner.
    with Image.open(io.BytesIO(out)) as res:
        px = res.convert("RGB").load()
        w, _h = res.size
        assert sum(px[w - 1, 0]) < sum(px[0, 0]), (px[w - 1, 0], px[0, 0])


def test_same_input_twice_byte_identical():
    from thumbnails import downscale_bytes

    src = _png_bytes(2000, 1500, color=(12, 34, 56))
    a, _ = downscale_bytes(src, "image/png")
    b, _ = downscale_bytes(src, "image/png")
    assert a == b


def test_corrupt_bytes_returns_original_no_raise(caplog):
    """Failure is non-fatal: a corrupt SAFE-format input returns the original bytes."""
    import logging

    from thumbnails import downscale_bytes

    corrupt = b"\x89PNG\r\n\x1a\n" + b"NOT-A-REAL-PNG-STREAM"
    with caplog.at_level(logging.WARNING):
        out, mime = downscale_bytes(corrupt, "image/png")
    assert out == corrupt
    assert mime == "image/png"


def test_untranscodable_webp_returns_none():
    """A WebP that cannot be transcoded is dropped (None) — never sent verbatim."""
    from thumbnails import downscale_bytes

    corrupt_webp = b"RIFF\x00\x00\x00\x00WEBP" + b"GARBAGE"
    assert downscale_bytes(corrupt_webp, "image/webp") is None


# ─── render_model_variant: cached + idempotent ────────────────────────────────


def test_render_model_variant_writes_and_returns(tmp_path):
    from thumbnails import get_model_variant_path, render_model_variant

    src = tmp_path / "src.png"
    src.write_bytes(_png_bytes(3000, 2000))
    pid, rid = "mv-proj", "mv-ref"

    # Point STORAGE_PATH at tmp so the variant lands under it.
    import thumbnails as t
    orig_sp = t.STORAGE_PATH
    t.STORAGE_PATH = tmp_path
    try:
        data, out_mime = render_model_variant(src, pid, rid, "image/png")
        assert out_mime == "image/png"
        assert _dims(data)[0] * _dims(data)[1] <= 1_250_000
        vpath = get_model_variant_path(pid, rid, out_mime)
        assert vpath.is_file()
        assert vpath.read_bytes() == data
    finally:
        t.STORAGE_PATH = orig_sp


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
        messages=[{"role": "user", "content": "hi"}],
        context_ids=["ref-strip"], model="deepseek/pro",
    )
    result = await ctx_module.build_context(body, "proj-1", {"document_id": None})

    assert not any(p.get("type") == "image_url" for p in result.image_parts), \
        result.image_parts
    assert any(w.get("code") == "image_not_delivered" for w in result.warnings), \
        result.warnings


async def test_vision_model_delivers_normalized_variant(monkeypatch, tmp_path):
    from routes.chat import context as ctx_module

    from config import MODEL_IMAGE_MAX_PIXELS, MODEL_IMAGE_SAFE_MIMES
    from models import CompletionRequest

    _ctx_image_ref(monkeypatch, tmp_path, ref_id="ref-deliver")
    _patch_vision(monkeypatch, True)

    body = CompletionRequest(
        messages=[{"role": "user", "content": "hi"}],
        context_ids=["ref-deliver"], model="local/orange/chat",
    )
    result = await ctx_module.build_context(body, "proj-1", {"document_id": None})

    url_parts = [p for p in result.image_parts if p.get("type") == "image_url"]
    assert len(url_parts) == 1, result.image_parts
    url = url_parts[0]["image_url"]["url"]
    # INVARIANT: the delivered mime is in the safe set — assert the mime the URI
    # actually declares, not a startswith() guess. Why: a `data:image/webp,` prefix
    # check can never fire (real URIs carry `;base64,` before the comma), so the
    # end-to-end "never WebP" contract was silently untested.
    assert _uri_mime(url) in MODEL_IMAGE_SAFE_MIMES, url
    # The delivered variant is within the pixel cap (downscaled, not the original).
    raw_b64 = url.split(",", 1)[1]
    data = base64.b64decode(raw_b64)
    w, h = _dims(data)
    assert w * h <= MODEL_IMAGE_MAX_PIXELS, (w, h)


async def test_webp_reference_is_transcoded_end_to_end(monkeypatch, tmp_path):
    """A WebP reference must reach the model as JPEG, through build_context.

    This is the path the live probe caught: a WebP part is silently dropped by the
    local models (23 prompt tokens = text-only) and the model then invents an
    answer about an image it never saw. Pinned end-to-end, not just on the
    downscale_bytes unit, because the delivery path is what regressed.
    """
    from routes.chat import context as ctx_module

    from config import MODEL_IMAGE_SAFE_MIMES
    from models import CompletionRequest

    _ctx_image_ref(monkeypatch, tmp_path, ref_id="ref-webp", mime="image/webp")
    _patch_vision(monkeypatch, True)

    body = CompletionRequest(
        messages=[{"role": "user", "content": "hi"}],
        context_ids=["ref-webp"], model="local/orange/chat",
    )
    result = await ctx_module.build_context(body, "proj-1", {"document_id": None})

    url_parts = [p for p in result.image_parts if p.get("type") == "image_url"]
    assert len(url_parts) == 1, result.image_parts
    url = url_parts[0]["image_url"]["url"]
    assert _uri_mime(url) == "image/jpeg", url
    assert _uri_mime(url) in MODEL_IMAGE_SAFE_MIMES
    # Payload is genuinely re-encoded, not a relabelled WebP.
    assert _format(base64.b64decode(url.split(",", 1)[1])) == "JPEG"


# ─── thumbnail serve: the warm must never block the response ──────────────────


async def test_thumbnail_response_does_not_wait_for_model_variant(monkeypatch, tmp_path):
    """INVARIANT: _serve_thumbnail returns without awaiting the variant pre-warm.

    Why pinned: rendering the variant is a full Pillow decode→LANCZOS→encode
    (measured 2.7s at 4000x3000). Awaited inline it delayed every first-time
    thumbnail by that much, so opening a reference gallery paid it once per image.
    The warm is pure optimization — chat context renders lazily on miss.

    Deterministic by construction: the fake warm blocks on an Event that this test
    controls, so the response can only arrive if it was never awaited. No timing
    assertions, nothing to flake.
    """
    import threading

    from routes import files_serve as fu

    src = tmp_path / "src.png"
    src.write_bytes(_png_bytes(64, 64))
    thumb = tmp_path / "_thumb.webp"

    released = threading.Event()
    warm_done = threading.Event()

    def blocking_warm(*_a, **_kw):
        released.wait(timeout=5)
        warm_done.set()

    async def fake_resolve(*, ref):  # noqa: ARG001
        return src, "image/png", True

    def fake_generate(_src, dst, *a, **kw):  # noqa: ARG001
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"webp-bytes")
        return (64, 64)

    monkeypatch.setattr(fu, "warm_model_variant", blocking_warm)
    monkeypatch.setattr(fu, "resolve_reference_file", fake_resolve)
    monkeypatch.setattr(fu, "generate_thumbnail", fake_generate)
    monkeypatch.setattr(fu, "get_thumb_path", lambda _p, _r: thumb)

    ref = {"media_type": "image", "project_id": "p1"}
    try:
        resp = await fu._serve_thumbnail(ref, "r1")
        assert resp.media_type == "image/webp"
        # The load-bearing assertion: the warm has NOT finished. An awaited warm
        # could only return after setting this, so this is what actually fails if
        # the `await` comes back. (Asserting `not released.is_set()` would NOT —
        # the blocked wait times out and the awaited version passes, just slower.)
        assert not warm_done.is_set(), "response waited for the model-variant warm"
    finally:
        released.set()


# ─── completions_turn: attachment normalization ───────────────────────────────


def _data_uri(w: int, h: int, mime: str = "image/png") -> str:
    fmt = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[mime]
    img = Image.new("RGB", (w, h), (5, 6, 7))
    if fmt == "JPEG":
        raw = _save_bytes(img, "JPEG")
    else:
        raw = _save_bytes(img, fmt)
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


async def test_attachment_downscaled_to_cap():
    from routes.chat.completions_turn import _build_last_user_prompt

    from config import MODEL_IMAGE_MAX_PIXELS, MODEL_IMAGE_SAFE_MIMES
    from models import ChatMessage

    big = _data_uri(4000, 3000)
    msg = ChatMessage(role="user", content="see this", images=[big])
    parts = await _build_last_user_prompt([msg], vision_ok=True)
    img_parts = [p for p in parts if p.get("type") == "image_url"]
    assert len(img_parts) == 1
    url = img_parts[0]["image_url"]["url"]
    assert _uri_mime(url) in MODEL_IMAGE_SAFE_MIMES, url
    data = base64.b64decode(url.split(",", 1)[1])
    w, h = _dims(data)
    assert w * h <= MODEL_IMAGE_MAX_PIXELS, (w, h)


async def test_attachment_webp_transcoded_to_jpeg():
    from routes.chat.completions_turn import _build_last_user_prompt

    from models import ChatMessage

    webp = _data_uri(120, 120, mime="image/webp")
    msg = ChatMessage(role="user", content="x", images=[webp])
    parts = await _build_last_user_prompt([msg], vision_ok=True)
    url = next(p for p in parts if p.get("type") == "image_url")["image_url"]["url"]
    assert _uri_mime(url) == "image/jpeg", url
    # The payload really is a JPEG, not just a relabelled WebP.
    assert _format(base64.b64decode(url.split(",", 1)[1])) == "JPEG"


async def test_attachment_normalization_runs_off_the_event_loop(monkeypatch):
    """INVARIANT: Pillow work must not run on the loop thread.

    Why pinned: _normalize_attachment is a full decode→LANCZOS→encode — measured
    242ms at 1600x1200 and 2.7s at 4000x3000, on EVERY turn (attachment variants
    are not cached). Run inline it froze WS collab, presence and all other HTTP
    for that whole time. Asserted by capturing the executing thread rather than by
    timing, so it cannot flake.
    """
    import threading

    from routes.chat import completions_turn as ct

    from models import ChatMessage

    seen: list[str] = []
    real = ct._normalize_attachment

    def spy(uri, **kw):
        seen.append(threading.current_thread().name)
        return real(uri, **kw)

    monkeypatch.setattr(ct, "_normalize_attachment", spy)

    msg = ChatMessage(role="user", content="x", images=[_data_uri(64, 64)])
    await ct._build_last_user_prompt([msg], vision_ok=True)

    assert seen, "normalization never ran"
    main = threading.main_thread().name
    assert all(t != main for t in seen), seen


async def test_attachment_non_vision_strips_images():
    from routes.chat.completions_turn import _build_last_user_prompt

    from models import ChatMessage

    msg = ChatMessage(role="user", content="x", images=[_data_uri(64, 64)])
    out = await _build_last_user_prompt([msg], vision_ok=False)
    # Exactly the plain-string shape — asserted unconditionally, not as an
    # `isinstance(...) or ...` disjunction that would pass on either outcome.
    assert out == "x", out
