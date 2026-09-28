"""ADTS-AAC upload support — validation relax + remux into a real M4A container.

Some voice recorders emit a raw ADTS-AAC stream under an .m4a extension (browser
reports it as audio/mp4 / audio/x-m4a). The old magic-byte check 400'd these.
See incident 2026-06-03 ("29 мая_ 16.49.m4a" was raw ADTS, not an MP4 container).
"""

import base64
import io
import subprocess
import tempfile
from pathlib import Path

import pytest

# A real 0.2s mono ADTS-AAC stream (ffmpeg: anullsrc -> aac -f adts). Starts FF F1.
ADTS_AAC_BYTES = base64.b64decode(
    "//FgQAOf/N4CAExhdmM2MS4xOS4xMDEAAjBADv/xYEABf/wBGCAH"
    "//FgQAF//AEYIAf/8WBAAX/8ARggB//xYEABf/wBGCAH"
)

# Minimal WebM EBML header — a non-ADTS audio upload that must pass through unchanged.
WEBM_BYTES = b"\x1aE\xdf\xa3" + b"\x00" * 96


# ─── WebM duration/seekability fixtures ─────────────────────────────────────
# MediaRecorder (browser) emits WebM with NO Duration element — ffprobe reports
# N/A and the player reads Infinity until the file is fully buffered. Replicate
# that artifact: mux silence to a PIPE with a frame cap and no -t, so the
# matroska muxer cannot compute duration up front. Stream-copying the same bytes
# to a seekable file (-c copy) bakes the Duration element in — the fix under test.
def _gen_durationless_webm() -> bytes:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
         "-frames:a", "300", "-c:a", "libopus", "-b:a", "32k",
         "-f", "webm", "pipe:1"],
        capture_output=True, check=True,
    )
    assert proc.stdout[:4] == b"\x1aE\xdf\xa3"
    return proc.stdout


def _remux_to_seekable(data: bytes) -> bytes:
    """Stream-copy remux to a seekable file → bakes in the Duration element."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.webm"
        dst = Path(tmp) / "out.webm"
        src.write_bytes(data)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(src), "-c", "copy", str(dst)],
            check=True, capture_output=True,
        )
        return dst.read_bytes()


WEBM_NO_DUR = _gen_durationless_webm()       # duration-less (replicates MediaRecorder)
WEBM_WITH_DUR = _remux_to_seekable(WEBM_NO_DUR)  # carries its own Duration element


# ─── Unit: _is_adts_aac ──────────────────────────────────────────────────────


def test_is_adts_aac_true_for_adts_stream():
    from files_util import _is_adts_aac
    assert _is_adts_aac(ADTS_AAC_BYTES) is True


def test_is_adts_aac_false_for_mp3_frame():
    # MP3 frame sync FF FB sets layer bits 01 (Layer III), not 00 (AAC).
    from files_util import _is_adts_aac
    assert _is_adts_aac(b"\xff\xfb\x90\x00") is False


def test_is_adts_aac_false_for_mp4_container():
    from files_util import _is_adts_aac
    assert _is_adts_aac(b"\x00\x00\x00\x1cftypM4A ") is False


def test_is_adts_aac_false_for_webm():
    from files_util import _is_adts_aac
    assert _is_adts_aac(WEBM_BYTES) is False


# ─── Unit: validate_magic ────────────────────────────────────────────────────


@pytest.mark.parametrize("mime", ["audio/mp4", "audio/x-m4a", "audio/m4a", "audio/aac", "audio/mpeg"])
def test_validate_magic_accepts_adts_under_m4a_family(mime):
    from files_util import validate_magic
    assert validate_magic(ADTS_AAC_BYTES, mime) is True


def test_validate_magic_rejects_adts_under_image_mime():
    from files_util import validate_magic
    assert validate_magic(ADTS_AAC_BYTES, "image/png") is False


def test_validate_magic_mp3_still_rejected_under_mp4():
    # A genuine MP3 stream must NOT be accepted as audio/mp4 (it isn't remuxable AAC).
    from files_util import validate_magic
    assert validate_magic(b"\xff\xfb\x90\x00" + b"\x00" * 64, "audio/mp4") is False


def test_validate_magic_real_m4a_still_accepted():
    from files_util import validate_magic
    assert validate_magic(b"\x00\x00\x00\x1cftypM4A ", "audio/mp4") is True


# ─── Unit: normalize_audio_upload (real ffmpeg remux) ────────────────────────


@pytest.mark.asyncio
async def test_normalize_remuxes_adts_into_real_m4a():
    from files_util import normalize_audio_upload
    data, mime, name = await normalize_audio_upload(ADTS_AAC_BYTES, "audio/mp4", "rec.m4a")
    # Output is a real MP4 container (ftyp box at offset 4), not the raw ADTS sync.
    assert data[4:8] == b"ftyp"
    assert data[:2] != b"\xff\xf1"
    assert mime == "audio/mp4"
    assert name.endswith(".m4a")


@pytest.mark.asyncio
async def test_normalize_renames_aac_extension_to_m4a():
    from files_util import normalize_audio_upload
    _, _, name = await normalize_audio_upload(ADTS_AAC_BYTES, "audio/aac", "voice.aac")
    assert name == "voice.m4a"


@pytest.mark.asyncio
async def test_normalize_passthrough_for_non_adts():
    from files_util import normalize_audio_upload
    data, mime, name = await normalize_audio_upload(WEBM_BYTES, "audio/webm", "clip.webm")
    assert data == WEBM_BYTES
    assert mime == "audio/webm"
    assert name == "clip.webm"


# ─── Unit: probe_duration_sec ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_duration_sec_none_for_durationless_webm(tmp_path):
    # MediaRecorder-style WebM: ffprobe reports N/A → None.
    from files_util import probe_duration_sec
    p = tmp_path / "nodur.webm"
    p.write_bytes(WEBM_NO_DUR)
    assert await probe_duration_sec(p) is None


@pytest.mark.asyncio
async def test_probe_duration_sec_finite_for_seekable_webm(tmp_path):
    # A WebM that carries a Duration element (the remuxed fixture) → finite > 0.
    from files_util import probe_duration_sec
    p = tmp_path / "ok.webm"
    p.write_bytes(WEBM_WITH_DUR)
    dur = await probe_duration_sec(p)
    assert dur is not None and dur > 0


@pytest.mark.asyncio
async def test_probe_duration_sec_none_for_corrupt_bytes(tmp_path):
    # EBML magic but no streams / truncated → ffprobe exits non-zero → None.
    # The upload must never fail because a probe did.
    from files_util import probe_duration_sec
    p = tmp_path / "garbage.webm"
    p.write_bytes(WEBM_BYTES)
    assert await probe_duration_sec(p) is None


# ─── Unit: normalize_audio_upload_path — WebM seekable branch ────────────────


@pytest.mark.asyncio
async def test_normalize_path_remuxes_durationless_webm(tmp_path):
    # A duration-less WebM is rewritten in place so the stored container reports
    # its own duration (player no longer reads Infinity before buffering).
    from files_util import normalize_audio_upload_path, probe_duration_sec
    p = tmp_path / "clip.webm"
    p.write_bytes(WEBM_NO_DUR)
    out_path, mime, name = await normalize_audio_upload_path(
        p, WEBM_NO_DUR[:16], "audio/webm", "clip.webm")
    assert out_path is p          # same path, rewritten in place
    assert mime == "audio/webm"   # mime unchanged (no re-encode)
    assert name == "clip.webm"
    assert await probe_duration_sec(p) is not None   # now reports a duration


@pytest.mark.asyncio
async def test_normalize_path_remuxes_durationless_webm_no_extension(tmp_path):
    # The real streamed upload temp file has NO extension (tempfile.mkstemp).
    # The remux must not depend on the input filename's suffix to pick a muxer —
    # this is the case the end-to-end upload exercises (caught a regression where
    # the output was named extension-less and ffmpeg refused the format).
    from files_util import normalize_audio_upload_path, probe_duration_sec
    p = tmp_path / "tmpupload_no_ext"
    p.write_bytes(WEBM_NO_DUR)
    out_path, mime, name = await normalize_audio_upload_path(
        p, WEBM_NO_DUR[:16], "audio/webm", "clip.webm")
    assert out_path is p
    assert await probe_duration_sec(p) is not None


@pytest.mark.asyncio
async def test_normalize_path_passthrough_webm_that_has_duration(tmp_path):
    # A WebM that already carries a Duration element is NOT remuxed — byte-identical.
    from files_util import normalize_audio_upload_path
    p = tmp_path / "ok.webm"
    p.write_bytes(WEBM_WITH_DUR)
    before = p.read_bytes()
    out_path, mime, name = await normalize_audio_upload_path(
        p, WEBM_WITH_DUR[:16], "audio/webm", "ok.webm")
    assert p.read_bytes() == before
    assert out_path is p
    assert mime == "audio/webm"


@pytest.mark.asyncio
async def test_normalize_path_keeps_original_when_remux_fails(tmp_path):
    # Truncated WebM: ffprobe returns None (duration-less) so remux is attempted,
    # but ffmpeg -c copy fails to open the input. The original bytes must survive
    # and no exception must propagate (degrade to today's behavior, never reject).
    from files_util import normalize_audio_upload_path
    truncated = WEBM_NO_DUR[:120]
    p = tmp_path / "trunc.webm"
    p.write_bytes(truncated)
    out_path, mime, name = await normalize_audio_upload_path(
        p, truncated[:16], "audio/webm", "trunc.webm")
    assert p.read_bytes() == truncated
    assert mime == "audio/webm"


@pytest.mark.asyncio
async def test_normalize_path_adts_still_remuxed(tmp_path):
    # The WebM branch must not break the existing ADTS-AAC remux path.
    from files_util import normalize_audio_upload_path
    p = tmp_path / "rec.m4a"
    p.write_bytes(ADTS_AAC_BYTES)
    out_path, mime, name = await normalize_audio_upload_path(
        p, ADTS_AAC_BYTES[:16], "audio/mp4", "rec.m4a")
    assert (p.read_bytes())[4:8] == b"ftyp"   # remuxed into a real MP4 container
    assert mime == "audio/mp4"
    assert name.endswith(".m4a")


# ─── Unit: save_upload — duration_sec in file_meta ───────────────────────────


@pytest.mark.asyncio
async def test_save_upload_stores_duration_sec_for_audio(tmp_path, monkeypatch):
    # Mirrors how images get width/height: audio references carry duration_sec,
    # probed from the file that will actually be stored.
    import files_util

    captured = {}

    async def fake_create_reference_row(**fields):
        captured.update(fields)
        return {"id": fields["ref_id"], **fields}

    monkeypatch.setattr(files_util, "create_reference_row", fake_create_reference_row)

    src = tmp_path / "in.webm"
    src.write_bytes(WEBM_WITH_DUR)
    _ref_id, _result = await files_util.save_upload(
        None, "audio/webm", "clip.webm", "ptest_audiodur", "ptest_audiodur-host",
        title="clip", media_type="audio", processing_status=None,
        src_path=src,
    )
    assert captured["file_meta"]["duration_sec"] > 0
    assert captured["file_meta"]["mime_type"] == "audio/webm"


@pytest.mark.asyncio
async def test_save_upload_no_duration_sec_for_markdown(monkeypatch):
    # duration_sec is audio-only; a markdown reference must not carry it.
    import files_util

    captured = {}

    async def fake_create_reference_row(**fields):
        captured.update(fields)
        return {"id": fields["ref_id"], **fields}

    monkeypatch.setattr(files_util, "create_reference_row", fake_create_reference_row)

    _ref_id, _result = await files_util.save_upload(
        b"# hello\nworld", "text/markdown", "note.md", "ptest_audiodur", "ptest_audiodur-host",
        title="note", media_type="markdown", processing_status=None,
    )
    assert "duration_sec" not in captured["file_meta"]
    assert captured["file_meta"]["mime_type"] == "text/markdown"


# ─── Integration: upload routes ──────────────────────────────────────────────


async def _make_doc(client, token, pid, title):
    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": title},
        cookies={"lore_session": token},
    )
    return resp.json()["document_id"]


@pytest.mark.asyncio
async def test_upload_adts_m4a_accepted_and_remuxed(client, admin_user, project_with_doc):
    from config import STORAGE_PATH
    from db import fetch_one

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "AdtsDoc")

    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("recording.m4a", io.BytesIO(ADTS_AAC_BYTES), "audio/mp4")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["media_type"] == "audio"

    ref = await fetch_one("documents", data["reference_id"])
    stored = (STORAGE_PATH / ref["file_path"]).read_bytes()
    assert stored[4:8] == b"ftyp"  # remuxed into a real container on disk


@pytest.mark.asyncio
async def test_upload_and_transcribe_adts_m4a(client, admin_user, project_with_doc):
    from unittest.mock import AsyncMock, patch

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "AdtsTranscribeDoc")

    with patch("routes.files.transcribe_audio", new_callable=AsyncMock, return_value="Hi"):
        resp = await client.post(
            "/api/documents/upload-and-transcribe",
            data={"project_id": pid, "document_id": doc_id},
            files={"file": ("voice.m4a", io.BytesIO(ADTS_AAC_BYTES), "audio/mp4")},
            cookies={"lore_session": token},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == "Hi"


@pytest.mark.asyncio
async def test_upload_durationless_webm_remuxed_and_carries_duration(client, admin_user, project_with_doc):
    """End-to-end acceptance: a duration-less MediaRecorder WebM uploaded through
    the real route is remuxed in place (the stored container reports its own
    duration) AND the reference carries file_meta.duration_sec — the badge lights
    up and the player knows the total on first open, before any buffering."""
    import subprocess

    from config import STORAGE_PATH
    from db import fetch_one

    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "WebmDurDoc")

    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("clip.webm", io.BytesIO(WEBM_NO_DUR), "audio/webm")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["media_type"] == "audio"
    assert data["file_meta"]["duration_sec"] > 0   # probed from the remuxed file

    # The STORED container now reports its own duration — no longer duration-less.
    ref = await fetch_one("documents", data["reference_id"])
    stored_path = STORAGE_PATH / ref["file_path"]
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(stored_path)],
        capture_output=True, text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert float(probe.stdout.strip()) > 0
