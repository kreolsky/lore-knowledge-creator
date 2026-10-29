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


# ─── Unit: validate_magic ────────────────────────────────────────────────────


def test_validate_magic_mp3_still_rejected_under_mp4():
    # A genuine MP3 stream must NOT be accepted as audio/mp4 (it isn't remuxable AAC).
    from files_util import validate_magic
    assert validate_magic(b"\xff\xfb\x90\x00" + b"\x00" * 64, "audio/mp4") is False


def test_validate_magic_real_m4a_still_accepted():
    from files_util import validate_magic
    assert validate_magic(b"\x00\x00\x00\x1cftypM4A ", "audio/mp4") is True


# ─── Unit: normalize_audio_upload (real ffmpeg remux) ────────────────────────


@pytest.mark.asyncio
async def test_normalize_renames_aac_extension_to_m4a():
    from files_util import normalize_audio_upload
    _, _, name = await normalize_audio_upload(ADTS_AAC_BYTES, "audio/aac", "voice.aac")
    assert name == "voice.m4a"


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
