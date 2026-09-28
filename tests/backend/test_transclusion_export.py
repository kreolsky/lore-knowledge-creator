"""SYSTEM: transclusion — export inlining of document / text-reference embeds."""

from unittest.mock import AsyncMock, patch

import pytest
from documents.service import _inline_ref_images, _inline_transclusions

# The exporting document's project — every mocked row carries it, every call
# passes it (the inliners' project wall takes the EXPORTING doc's project).
_PROJ = "p1"


def _doc(content, is_reference=False, file_path=None, deleted_at=None):
    return {
        "project_id": _PROJ,
        "content": content,
        "is_reference": is_reference,
        "file_path": file_path,
        "deleted_at": deleted_at,
    }


@pytest.mark.asyncio
async def test_inlines_doc_embed_as_text():
    fake = AsyncMock(return_value=_doc("# Embedded\nbody"))
    with patch("documents.service.fetch_one", fake):
        out = await _inline_transclusions("before\n![T](doc:d1)\nafter", _PROJ)
    assert out == "before\n# Embedded\nbody\nafter"


@pytest.mark.asyncio
async def test_inlines_bare_id_doc_embed():
    fake = AsyncMock(return_value=_doc("plain text"))
    with patch("documents.service.fetch_one", fake):
        out = await _inline_transclusions("![T](d2)", _PROJ)
    assert out == "plain text"


@pytest.mark.asyncio
async def test_inlines_text_reference_embed():
    fake = AsyncMock(return_value=_doc("note content", is_reference=True))
    with patch("documents.service.fetch_one", fake):
        out = await _inline_transclusions("![R](ref:r1)", _PROJ)
    assert out == "note content"


@pytest.mark.asyncio
async def test_skips_image_reference_for_base64_pass():
    # An image reference is left untouched so _inline_ref_images base64-inlines it after.
    ref = {"content": None, "is_reference": True, "file_path": "img/x.png",
           "media_type": "image", "deleted_at": None, "project_id": _PROJ}
    with patch("documents.service.fetch_one", AsyncMock(return_value=ref)):
        out = await _inline_transclusions("![I](ref:img1)", _PROJ)
    assert out == "![I](ref:img1)"


@pytest.mark.asyncio
async def test_inlines_audio_transcription_as_text():
    # An audio reference carries a file AND a transcription — the transcription text is
    # inlined (it renders as a text band in the editor); the file is NOT base64'd here.
    ref = {"content": "transcribed words", "is_reference": True, "file_path": "a/v.webm",
           "media_type": "audio", "deleted_at": None, "project_id": _PROJ}
    with patch("documents.service.fetch_one", AsyncMock(return_value=ref)):
        out = await _inline_transclusions("![voice](ref:aud1)", _PROJ)
    assert out == "transcribed words"


# Bug G: a soft-deleted text-reference must NOT inline into export (mirrors the bare/doc
# branch which already guards deleted_at). This is the SOLE deletion-bug coverage — the
# frontend never sees deleted refs (storeReferences excludes them).
@pytest.mark.asyncio
async def test_skips_soft_deleted_text_reference():
    deleted_ref = _doc("ghost content", is_reference=True, deleted_at="2026-06-01T00:00:00Z")
    with patch("documents.service.fetch_one", AsyncMock(return_value=deleted_ref)):
        out = await _inline_transclusions("![R](ref:ghost)", _PROJ)
    assert out == "![R](ref:ghost)"


@pytest.mark.asyncio
async def test_skips_data_uri_target():
    # The shared reject regex must include `data:` — a data: URI is never a doc id.
    with patch("documents.service.fetch_one", AsyncMock(return_value=None)):
        out = await _inline_transclusions("![x](data:image/png;base64,AAAA)", _PROJ)
    assert out == "![x](data:image/png;base64,AAAA)"


@pytest.mark.asyncio
async def test_leaves_external_and_unresolvable_targets():
    fake = AsyncMock(return_value=None)
    with patch("documents.service.fetch_one", fake):
        out = await _inline_transclusions("![x](https://a/b.png)\n![y](doc:missing)", _PROJ)
    assert out == "![x](https://a/b.png)\n![y](doc:missing)"


@pytest.mark.asyncio
async def test_one_level_only_nested_embed_left_raw():
    # The inlined doc body itself contains an embed — it is NOT recursively resolved.
    fake = AsyncMock(return_value=_doc("outer\n![N](doc:inner)"))
    with patch("documents.service.fetch_one", fake):
        out = await _inline_transclusions("![T](doc:outer)", _PROJ)
    assert out == "outer\n![N](doc:inner)"


# ─── _inline_ref_images: base64 for images only, drop other media ───────────

def _img_ref(mime, path="f/x.bin"):
    return {"is_reference": True, "file_path": path, "file_meta": {"mime_type": mime},
            "project_id": _PROJ}


@pytest.mark.asyncio
async def test_image_ref_is_base64_inlined(tmp_path, monkeypatch):
    import config
    f = tmp_path / "x.png"
    f.write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(config, "STORAGE_PATH", tmp_path)
    fake = AsyncMock(return_value=_img_ref("image/png", "x.png"))
    with patch("documents.service.fetch_one", fake):
        out = await _inline_ref_images("![p](ref:img1)", _PROJ)
    assert out.startswith("![p](data:image/png;base64,")


@pytest.mark.asyncio
async def test_audio_ref_is_dropped_not_base64(tmp_path, monkeypatch):
    import config
    f = tmp_path / "a.webm"
    f.write_bytes(b"audio-bytes")
    monkeypatch.setattr(config, "STORAGE_PATH", tmp_path)
    fake = AsyncMock(return_value=_img_ref("audio/webm", "a.webm"))
    with patch("documents.service.fetch_one", fake):
        out = await _inline_ref_images("x ![voice](ref:aud1) y", _PROJ)
    assert "base64" not in out
    assert out == "x  y"
