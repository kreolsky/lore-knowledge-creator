"""Shared reference/upload utilities — file validation, filename → MIME,
persistence, and the audio normalization chain. Service code, not a route: the
reference-file serving chain lives in routes/files_serve.py, the resolution
chain (resolve_reference_file) in files_service.py.

# ARCH: callers reach shared upload helpers through ONE module instead of crossing
# into each other's private symbols. Consumers: routes/files.py (authed router),
# routes/public_share.py (anonymous router), widget.py (API-key auth), mcp_gateway,
# routes/tool_api. No private-symbol (_…) crossing module boundaries that is not
# flagged with an INVARIANT(anonymous-surface) in files_serve.py.
"""

import asyncio
import logging
import mimetypes
import os
import re
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import settings
from documents.service import create_reference_row, resolve_reference_host
from fastapi import HTTPException, UploadFile

from config import (
    AUDIO_MIMES,
    IMAGE_MIMES,
    STORAGE_PATH,
)
from db import serialize_record
from jobs import pool as jobs_pool

logger = logging.getLogger(__name__)

# Stream audio uploads to disk in 1 MB chunks instead of buffering the whole payload
# (up to MAX_AUDIO_SIZE_MB) in memory. The magic check only needs the first 16 bytes.
AUDIO_CHUNK_SIZE = 1024 * 1024
_MAGIC_HEAD_SIZE = 16
TMP_UPLOAD_DIR = STORAGE_PATH / "tmp"
# Orphan temp uploads (from a SIGKILL/OOM mid-stream) older than this are swept at
# startup. Above any realistic single-upload duration, so a live upload is never hit.
_TMP_ORPHAN_AGE_SEC = 3600


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to unlink temp upload %s", path, exc_info=True)


def _sweep_tmp_uploads_sync() -> int:
    """Delete orphan temp uploads older than _TMP_ORPHAN_AGE_SEC. Returns count removed."""
    if not TMP_UPLOAD_DIR.is_dir():
        return 0
    cutoff = time.time() - _TMP_ORPHAN_AGE_SEC
    removed = 0
    with os.scandir(TMP_UPLOAD_DIR) as it:
        for entry in it:
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
                    removed += 1
            except OSError:
                logger.warning("Failed to sweep temp upload %s", entry.path, exc_info=True)
    return removed


async def sweep_tmp_uploads() -> None:
    """Startup sweep of STORAGE_PATH/tmp.

    The streaming audio path (stream_upload_to_tmp) unlinks its temp file on every
    request-level error, but a hard crash (SIGKILL/OOM) mid-stream leaves an orphan on
    the persistent storage volume with no reaper. This clears stale ones on boot.
    """
    try:
        removed = await asyncio.to_thread(_sweep_tmp_uploads_sync)
        if removed:
            logger.info("Swept %d orphan temp upload(s) from %s", removed, TMP_UPLOAD_DIR)
    except Exception:
        logger.warning("Temp-upload sweep failed", exc_info=True)


MAGIC_SIGNATURES: list[tuple[bytes, set[str]]] = [
    (b"\xff\xd8\xff", {"image/jpeg"}),
    (b"\x89PNG", {"image/png"}),
    (b"GIF8", {"image/gif"}),
    (b"RIFF", {"image/webp", "audio/wav", "audio/x-wav"}),
    (b"\x1aE\xdf\xa3", {"audio/webm", "video/webm"}),
    (b"OggS", {"audio/ogg", "audio/opus"}),
    (b"ID3", {"audio/mpeg", "audio/mp3"}),
]

# WHY: text references are validated by CONTENT, not extension — any UTF-8 file whose
# bytes look like text may be uploaded. A NUL byte or a high share of non-printable
# control chars marks binary that happens to UTF-8-decode (e.g. some image headers).
_TEXT_CTRL_THRESHOLD = 0.30
_ALLOWED_CTRL = {0x09, 0x0A, 0x0D}  # tab, LF, CR


def is_text_bytes(data: bytes) -> str | None:
    """Return the decoded text if `data` looks like a UTF-8 text file, else None."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in text:
        return None
    if not text:
        return text
    ctrl = sum(1 for ch in text if ord(ch) < 0x20 and ord(ch) not in _ALLOWED_CTRL)
    if ctrl / len(text) > _TEXT_CTRL_THRESHOLD:
        return None
    return text


MP3_MIMES = {"audio/mpeg", "audio/mp3"}
FTYP_MIMES = {"audio/mp4", "audio/m4a", "audio/x-m4a", "audio/aac", "audio/3gpp", "audio/3gpp2"}
# WebM (audio/webm from the recorder, video/webm seen on real refs) is this app's
# primary recorded-audio container. MediaRecorder emits it WITHOUT a Duration
# element, so every consumer reads Infinity until the file is fully buffered —
# see probe_duration_sec / the WebM branch in normalize_audio_upload_path.
WEBM_MIMES = {"audio/webm", "video/webm"}

# A .docx is a ZIP container (Office Open XML); its magic is the ZIP local-file-header.
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# PDF import (pymupdf4llm in the converter). Magic is the %PDF- header.
PDF_MIME = "application/pdf"
_PDF_MAGIC = b"%PDF-"
# Agent-shared archive (import_file .zip only — NOT a widget upload type; see
# classify_upload_kind's non-widening pin on detect_media_type).
ARCHIVE_MIME = "application/zip"
_ZIP_MAGIC = b"PK\x03\x04"


def _is_mpeg_sync(data: bytes) -> bool:
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _is_adts_sync(data: bytes) -> bool:
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xF0) == 0xF0


def _is_adts_aac(data: bytes) -> bool:
    """True for a raw ADTS-AAC stream (syncword 0xFFF + layer bits 00).

    Layer 00 distinguishes AAC from MP3, which shares the 0xFFF syncword but sets
    layer 01 (Layer III). Used both to relax validation and to decide remuxing.
    """
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xF6) == 0xF0


def validate_magic(data: bytes, claimed_mime: str) -> bool:
    """Check file magic bytes against claimed MIME type."""
    # WHY: .docx and .pdf are the only non-audio/image uploads that carry a magic
    # check — gate them first since their signatures collide with nothing below.
    if claimed_mime == DOCX_MIME:
        return data[:len(_ZIP_MAGIC)] == _ZIP_MAGIC
    if claimed_mime == PDF_MIME:
        return data[:len(_PDF_MAGIC)] == _PDF_MAGIC
    # Archive: accept only the local-file-header signature — an empty archive
    # (PK\x05\x06) carries nothing and is rejected.
    if claimed_mime == ARCHIVE_MIME:
        return data[:len(_ZIP_MAGIC)] == _ZIP_MAGIC
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return claimed_mime in FTYP_MIMES
    for magic, allowed_mimes in MAGIC_SIGNATURES:
        if data[:len(magic)] == magic:
            return claimed_mime in allowed_mimes
    if _is_mpeg_sync(data):
        if _is_adts_sync(data):
            # INVARIANT: a raw ADTS-AAC stream may arrive under an .m4a/.mp4 extension
            # (some voice recorders emit AAC that way) → accept the MP4/AAC family, not
            # just audio/aac. normalize_audio_upload remuxes it into a real container.  Why: some recorders emit raw ADTS-AAC with an m4a/mp4 extension; accepting the AAC family (not just audio/aac) lets normalize remux it instead of 400'ing a genuine recording.
            # Why: rejecting these 400'd genuine user recordings (incident 2026-06-03,
            # ADTS-as-m4a "29 мая_ 16.49.m4a"). MP3 (layer 01) stays excluded from the family.
            if _is_adts_aac(data):
                return claimed_mime in (MP3_MIMES | {"audio/aac"} | FTYP_MIMES)
            return claimed_mime in (MP3_MIMES | {"audio/aac"})
        return claimed_mime in MP3_MIMES
    return False


# mime → extension for image references (image-gen output naming). Sibling of
# detect_image_mime; kept here so magic detection + extension mapping live together.
# WHY the keys are exactly IMAGE_MIMES: detect_image_mime only returns members of
# IMAGE_MIMES, so this map must cover every one of them (else save_upload naming
# would KeyError). Mirrors files.py _MIME_EXTENSIONS' jpg-not-jpeg convention.
IMAGE_EXT_BY_MIME: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def detect_image_mime(data: bytes) -> str | None:
    """Detect the image MIME from `data`'s magic bytes, restricted to the accepted
    IMAGE_MIMES set; None when the bytes are not a recognized image.

    Sibling of validate_magic: same MAGIC_SIGNATURES table, but DETECTS rather than
    confirms a claimed type. Used by the ComfyUI generate_image handler, which
    receives bytes with no trustworthy MIME from /view and must both validate AND
    stamp the right mime onto the saved reference.

    For a magic shared between image + audio (RIFF), the intersection with
    IMAGE_MIMES is always singular (webp only), so a WAV-prefixed blob would report
    webp — acceptable in the image-gen context (ComfyUI /view only serves images)
    and consistent with validate_magic's RIFF handling."""
    for magic, allowed in MAGIC_SIGNATURES:
        if data[:len(magic)] == magic:
            hit = allowed & IMAGE_MIMES
            if hit:
                return next(iter(hit))
    return None


async def remux_adts_to_m4a(data: bytes) -> bytes:
    """Wrap a raw ADTS-AAC stream in an MP4/M4A container via stream copy (no re-encode).

    Uses temp files so ffmpeg gets seekable output and writes a standard +faststart moov
    (browser-playable), rather than a fragmented pipe output. ffmpeg is a hard dependency
    of the worker image; raises RuntimeError if the remux fails.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.aac"
        dst = Path(tmp) / "out.m4a"
        await asyncio.to_thread(src.write_bytes, data)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 or not dst.exists():
            raise RuntimeError(f"ffmpeg ADTS remux failed: {err.decode('utf-8', 'replace')}")
        return await asyncio.to_thread(dst.read_bytes)


async def probe_duration_sec(path: Path) -> float | None:
    """Probe a media file's CONTAINER duration via ffprobe.

    Returns the duration in seconds, or None on any failure OR when the container
    carries no Duration element — the duration-less MediaRecorder WebM case
    (`ffprobe … format=duration` prints `N/A`). Best-effort by contract: callers
    must treat None as "unknown" and never fail an upload because of it.

    Reads `format=duration` (not a stream-level audio duration) so a multi-stream
    `video/webm` ref reports the whole container, matching what a browser/ffmpeg
    downstream would see.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
    except Exception:
        logger.warning("ffprobe invocation failed for %s", path, exc_info=True)
        return None
    if proc.returncode != 0:
        return None
    raw = out.decode("utf-8", "replace").strip()
    if raw in ("", "N/A"):
        return None
    try:
        d = float(raw)
    except ValueError:
        return None
    return d if d > 0 else None


async def remux_stream_copy_inplace(path: Path) -> bool:
    """Stream-copy remux `path` to a temp file and replace it in place.

    Used to bake a Duration element (and a seekable index) into a duration-less
    WebM container produced by MediaRecorder, so the stored file reports its own
    duration to every consumer (our player, a browser opening the download,
    ffmpeg downstream). Stream copy is I/O-bound and copies every stream, so a
    `video/webm` ref is handled too.

    Returns True on success. On any ffmpeg failure the ORIGINAL file is left
    untouched and False is returned — degrade to today's behavior, never reject
    the upload. Why remux happens in the upload path and never in a worker: see
    the write-once + immutable-cache INVARIANT (REFERENCE_FILE_CACHE_CONTROL in
    routes/files_serve.py).
    """
    with tempfile.TemporaryDirectory(dir=str(path.parent)) as tmp:
        # INVARIANT(same-fs): the temp dir is created in `path.parent` (STORAGE_PATH/tmp,  Why: os.replace is atomic only within one filesystem; STORAGE_PATH is a bind mount while the default tempdir lands on the container overlay, so the temp dir is placed in path.parent to keep the replace same-device.
        # where the streamed upload already lives) so the os.replace below stays on ONE
        # filesystem. STORAGE_PATH is a bind mount (./storage:/storage) while the default
        # TemporaryDirectory() lands in the container's /tmp overlay — a cross-device
        # os.replace raises OSError, which is uncaught here and would 500 the upload,
        # violating "never reject the upload because a remux did". Why: the e2e test gave
        # a false green because the test container keeps /storage and /tmp on the same
        # overlay fs; prod does not.
        # INVARIANT(format): name the output with an explicit webm container + `-f webm`.  Why: the streamed tmp upload (tempfile.mkstemp) has no extension, so ffmpeg can't infer the muxer from path.suffix and the remux would silently fail (duration_sec never written); the explicit -f webm forces the format.
        # The streamed tmp upload (stream_upload_to_tmp → tempfile.mkstemp) has NO
        # extension, so deriving the muxer from `path.suffix` leaves ffmpeg unable to
        # choose a format → remux silently fails and duration_sec is never written.
        # This branch only runs for WebM, so the container is fixed.
        dst = Path(tmp) / "remux.webm"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path), "-c", "copy", "-f", "webm", str(dst),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 or not dst.is_file():
            logger.warning(
                "Stream-copy remux failed (keeping original %s): %s",
                path, err.decode("utf-8", "replace"))
            return False
        try:
            await asyncio.to_thread(os.replace, dst, path)
        except OSError:
            # Same-fs is guaranteed by dir=path.parent above, but any replace
            # failure still degrades to the original rather than 500'ing the upload.
            logger.warning("os.replace after remux failed (keeping original %s)", path, exc_info=True)
            return False
        return True


async def normalize_audio_upload(data: bytes, mime: str, original_name: str) -> tuple[bytes, str, str]:
    """Remux raw ADTS-AAC uploads into a real M4A; pass every other audio through unchanged.

    Returns the (possibly new) bytes, MIME, and filename. Callers must use the returned
    name for both save_upload and any disk-path reconstruction.
    """
    if not _is_adts_aac(data):
        return data, mime, original_name
    remuxed = await remux_adts_to_m4a(data)
    stem = re.sub(r"\.[^.]+$", "", original_name) or original_name
    return remuxed, "audio/mp4", f"{stem}.m4a"


async def stream_upload_to_tmp(file: UploadFile, max_bytes: int, limit_mb: int) -> tuple[Path, bytes]:
    """Stream an UploadFile to a temp file in 1 MB chunks, aborting at max_bytes.

    Returns (tmp_path, head_bytes). Counting actual bytes closes the "Content-Length
    lies" gap for the biggest payload class. The temp file lives under STORAGE_PATH/tmp
    (same filesystem as the reference dirs, so save_upload can os.replace it into place);
    the caller owns it and must move it (save_upload(src_path=…)) or unlink it.
    Raises HTTPException 413 when the stream exceeds max_bytes.
    """
    await asyncio.to_thread(TMP_UPLOAD_DIR.mkdir, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=TMP_UPLOAD_DIR)
    tmp_path = Path(tmp_name)
    head = b""
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(AUDIO_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail=f"File too large (max {limit_mb}MB)")
                if len(head) < _MAGIC_HEAD_SIZE:
                    head += chunk[: _MAGIC_HEAD_SIZE - len(head)]
                await asyncio.to_thread(out.write, chunk)
    except BaseException:
        _safe_unlink(tmp_path)
        raise
    return tmp_path, head


async def normalize_audio_upload_path(
    tmp_path: Path, head: bytes, mime: str, original_name: str
) -> tuple[Path, str, str]:
    """Path-based sibling of normalize_audio_upload — remux raw ADTS-AAC in place,
    and bake a Duration element into duration-less WebM.

    Decides solely from the 16-byte head (ADTS sync is in the first 2 bytes) for
    the ADTS path. Only the rare remux paths read the whole file back; every
    other audio passes straight through without leaving disk.
    """
    if _is_adts_aac(head):
        data = await asyncio.to_thread(tmp_path.read_bytes)
        remuxed = await remux_adts_to_m4a(data)
        await asyncio.to_thread(tmp_path.write_bytes, remuxed)
        stem = re.sub(r"\.[^.]+$", "", original_name) or original_name
        return tmp_path, "audio/mp4", f"{stem}.m4a"
    # ARCH: WebM seekability. MediaRecorder emits WebM with NO Duration element,
    # so the browser reports `duration === Infinity` until the whole file is
    # buffered — the seek bar is dead and the total time unknown (AudioPlayer.tsx).
    # If the stored container is duration-less, stream-copy remux bakes the
    # Duration in. Detected by PROBE, not by mime: a WebM that already carries a
    # duration passes through byte-identical, and non-WebM audio never reaches
    # the probe (every other container reports duration natively).
    if mime in WEBM_MIMES and await probe_duration_sec(tmp_path) is None:
        await remux_stream_copy_inplace(tmp_path)
    return tmp_path, mime, original_name


async def save_audio_upload(
    file: UploadFile, mime: str, original_name: str,
    project_id: str, document_id: str | None, *,
    title: str, processing_status: str,
    created_by: str | None = None,
    created_by_name: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[str, dict, Path]:
    """Stream an audio UploadFile to disk, validate, optionally remux, and persist.

    The full streaming audio path shared by all three upload routes: caps actual bytes
    at MAX_AUDIO_SIZE_MB, validates magic on the head, remuxes raw ADTS-AAC, then moves
    the temp file into the reference dir — never holding the whole payload in memory.
    Returns (ref_id, serialized_record, stored_file_path).
    """
    max_audio_mb = await settings.get("MAX_AUDIO_SIZE_MB")
    tmp_path, head = await stream_upload_to_tmp(file, max_audio_mb * 1024 * 1024, max_audio_mb)
    try:
        if not validate_magic(head, mime):
            logger.warning("Audio upload rejected — magic mismatch: MIME=%s, header=%s", mime, head.hex())
            raise HTTPException(status_code=400, detail="File content does not match declared type")
        tmp_path, mime, original_name = await normalize_audio_upload_path(tmp_path, head, mime, original_name)
        ref_id, result = await save_upload(
            None, mime, original_name, project_id, document_id,
            title=title, media_type="audio", processing_status=processing_status,
            src_path=tmp_path,
            created_by=created_by, created_by_name=created_by_name,
            idempotency_key=idempotency_key,
        )
    except BaseException:
        _safe_unlink(tmp_path)
        raise
    file_path = STORAGE_PATH / project_id / ref_id / re.sub(r'[^\w.\-]', '_', original_name)
    return ref_id, result, file_path


def detect_media_type(mime: str) -> str | None:
    if mime in AUDIO_MIMES:
        return "audio"
    if mime in IMAGE_MIMES:
        return "image"
    return None


async def classify_upload_kind(filename: str, mime: str) -> tuple[str | None, int]:
    """Map a filename/mime to an upload KIND + its size cap.

    D5: the MCP byte channel carries docx + markdown
    alongside audio/image. `detect_media_type` is SHARED with the widget upload route
    and returns only audio/image/None — it must NOT be widened silently (the widget
    would then accept new types too). This classifier ADDS docx/markdown categories
    for the MCP edge, shared by the mint (mcp_gateway.dispatch) and the redeem
    (routes.files_mcp_upload) so the two edges of one operation cannot drift. PDF
    (export-only) and anything else returns (None, 0) → refused. Returns (kind, max_mb).
    """
    mt = detect_media_type(mime)
    caps = await settings.get_all([
        "MAX_AUDIO_SIZE_MB", "MAX_IMAGE_SIZE_MB", "MAX_DOCX_SIZE_MB",
        "MAX_MARKDOWN_SIZE_MB",
    ])
    if mt == "audio":
        return "audio", caps["MAX_AUDIO_SIZE_MB"]
    if mt == "image":
        return "image", caps["MAX_IMAGE_SIZE_MB"]
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".docx":
        return "docx", caps["MAX_DOCX_SIZE_MB"]
    if ext in (".md", ".markdown"):
        return "markdown", caps["MAX_MARKDOWN_SIZE_MB"]
    return None, 0


async def save_upload(
    data: bytes | None,
    mime: str,
    original_name: str,
    project_id: str,
    document_id: str | None,
    title: str,
    media_type: str,
    processing_status: str | None,
    *,
    src_path: Path | None = None,
    created_by: str | None = None,
    created_by_name: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[str, dict]:
    """Save uploaded file to disk, create a reference-document via the shared service.

    Pass `data` for in-memory uploads (images, markdown, docx) or `src_path` for a
    file already streamed to a temp file (audio) — the latter is moved into place
    without re-buffering. Exactly one of the two must be provided.
    """
    ref_id = str(uuid4())
    safe_name = re.sub(r'[^\w.\-]', '_', original_name)

    ref_dir = STORAGE_PATH / project_id / ref_id
    ref_dir.mkdir(parents=True, exist_ok=True)
    file_path = ref_dir / safe_name
    if src_path is not None:
        await asyncio.to_thread(os.replace, src_path, file_path)
        file_size = await asyncio.to_thread(lambda: file_path.stat().st_size)
    else:
        await asyncio.to_thread(file_path.write_bytes, data)
        file_size = len(data)

    relative_path = f"{project_id}/{ref_id}/{safe_name}"
    file_meta: dict = {
        "mime_type": mime,
        "file_size": file_size,
        "original_name": original_name,
    }

    if media_type == "image":
        try:
            from PIL import Image as _PILImage
            img = await asyncio.to_thread(_PILImage.open, file_path)
            orig_w, orig_h = img.size
            file_meta["width"] = orig_w
            file_meta["height"] = orig_h
        except Exception:
            logger.warning("Failed to read image dimensions for ref %s", ref_id, exc_info=True)

    # ARCH: audio duration is probed from the file that will actually be stored
    # (after the WebM remux above), mirroring how images get width/height here.
    # Best-effort: probe_duration_sec returns None on any failure / duration-less
    # container → the key is simply omitted (status badge + player fall back to
    # the streaming ceiling). The upload must never fail because a probe did.
    if media_type == "audio":
        dur = await probe_duration_sec(file_path)
        if dur is not None:
            file_meta["duration_sec"] = dur

    # "project level" upload (no host selected) → host on the project index doc, so
    # the reference is visible from every panel and survives the reference-host invariant
    # event. INVARIANT: paired with the documents_reference_parent_check schema event.
    # WHY: ref_id is generated up top (BEFORE the service call) and passed in.
    # Why: the storage dir + relative_path are already derived from it, so the service
    # must accept a caller-owned id and never re-derive one (a fresh id would orphan the
    # file just written under ref_id on disk).
    host_id = await resolve_reference_host(document_id, project_id)

    record = await create_reference_row(
        ref_id=ref_id, project_id=project_id, host_id=host_id,
        title=title, media_type=media_type,
        processing_status=processing_status,
        file_path=relative_path, file_meta=file_meta,
        created_by=created_by, created_by_name=created_by_name,
        idempotency_key=idempotency_key,
    )

    result = serialize_record(record, "reference_id")

    if media_type == "image":
        await jobs_pool.enqueue("thumbnail_task", project_id, ref_id, relative_path,
                      job_id=f"thumb:{ref_id}")

    return ref_id, result


def mime_for_filename(filename: str) -> str:
    """Resolve a MIME from the filename extension for a binary upload.

    Reuses transcription's authoritative audio extension map (the single source —
    mimetypes.guess_type returns None for .m4a/.ogg/.opus and video/webm for .webm),
    plus an explicit IMAGE extension map and an ARCHIVE map (.zip), falling back to
    the stdlib mimetypes DB for anything else. The result is later CONFIRMED by
    validate_magic, so a wrong extension → magic mismatch → 400.

    # WHY: webp is supported
    # everywhere else in the stack (config.IMAGE_MIMES, the magic check here +
    # IMAGE_EXT_BY_MIME, thumbnails), but the stdlib mimetypes DB in this image has
    # no webp entry, so a correctly-named .webp died here as 'Unsupported binary
    # type'. The frontend emits chat attachments AS webp, so the hole had to close
    # before D1 (mime-derived extension) could land. An explicit local map mirrors
    # the existing _EXT_TO_MIME precedent (audio) rather than a process-global
    # mimetypes.add_type — a global mutation for one caller is a worse seam.
    """
    from transcription import _EXT_TO_MIME

    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _EXT_TO_MIME:
        return _EXT_TO_MIME[ext]
    if ext in _IMAGE_EXT_TO_MIME:
        return _IMAGE_EXT_TO_MIME[ext]
    if ext in _ARCHIVE_EXT_TO_MIME:
        return _ARCHIVE_EXT_TO_MIME[ext]
    return mimetypes.guess_type(filename or "")[0] or ""


# Image extension → MIME. The keys are exactly the image types config accepts
# (jpeg/png/gif/webp); mirrors transcription._EXT_TO_MIME's local-map pattern. Why a
# local dict, not mimetypes.add_type: see mime_for_filename above.
_IMAGE_EXT_TO_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Archive extension → MIME (agent-shared .zip). Same local-map pattern as
# _IMAGE_EXT_TO_MIME: explicit, immune to stdlib DB variance.
_ARCHIVE_EXT_TO_MIME: dict[str, str] = {
    ".zip": ARCHIVE_MIME,
}
