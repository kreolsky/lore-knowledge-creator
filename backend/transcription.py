"""Audio→text transcription — STT API client + arq enqueue helper.

The actual transcription runs in the arq worker process (`jobs.tasks.transcribe_task`),
which has no live collab session and reaches editors via the Redis backplane.
This module keeps the STT HTTP client (`transcribe_audio`) used by the worker and
by the synchronous upload-and-transcribe endpoints.
"""
# SYSTEM: transcription — STT API client + arq job trigger

import asyncio
import logging
from pathlib import Path

import http_clients
import settings

from jobs import pool as jobs_pool

logger = logging.getLogger(__name__)

# WHY: mimetypes.guess_type returns video/webm for .webm — we need audio/* MIME types for the STT API
_EXT_TO_MIME: dict[str, str] = {
    ".webm": "audio/webm", ".ogg": "audio/ogg", ".opus": "audio/opus", ".wav": "audio/wav",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
}

MAX_RETRIES = 3

# STT HTTP budget: a ~1h upload transcribes for tens of minutes — the shared
# pool client ("transcription", SYSTEM: http-clients) is built with this bound;
# the worker warms it at startup.
STT_TIMEOUT_S = 1800


async def enqueue_transcription(user_id: str, reference_id: str) -> None:
    """Submit an audio reference for transcription via the arq worker queue.

    Callers persist `processing_status='queued'` on the ref before calling.
    """
    # WHY: job_id=transcribe:{ref} dedups concurrent duplicates (a re-record
    # or stuck-recovery while a job is in flight is a no-op). Why: replaces the old
    # per-user-queue dedup; arq frees the id once the job reaches a terminal state,
    # so a later re-record naturally starts a fresh run.
    # WHY: queue='transcription' isolates STT on a worker bounded by
    # STT_CONCURRENCY (max_jobs). Why: bounds global STT concurrency (decision #4).
    await jobs_pool.enqueue("transcribe_task", user_id, reference_id,
                  job_id=f"transcribe:{reference_id}", queue=jobs_pool.TRANSCRIPTION_QUEUE)


async def transcribe_audio(abs_path: Path) -> str:
    """Call Whisper STT API and return transcribed text.

    Sends audio in its original format with retry and exponential backoff.
    Raises RuntimeError if all retries fail.
    """
    send_mime = _EXT_TO_MIME.get(abs_path.suffix.lower(), "application/octet-stream")

    # STT_API_URL/KEY are folded off AI_API_* in config.py — the registry
    # fallback spells that fold so a row on the base reaches this worker reader.
    stt_url = await settings.get("STT_API_URL")
    stt_key = await settings.get("STT_API_KEY")
    stt_model = await settings.get("STT_MODEL")

    def _read_file_sync(path: Path) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    client = http_clients.get_http_client("transcription", timeout=STT_TIMEOUT_S)
    # WHY: capture the most informative detail from each attempt so the final
    # RuntimeError carries the STT error body (not just the HTTP status) —
    # raise_for_status()'s __str__ drops response.text, leaving the real cause
    # (e.g. provider "Failed to load audio") invisible in worker logs.
    last_detail = "no attempt completed"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = await asyncio.to_thread(_read_file_sync, abs_path)
            response = await client.post(
                    f"{stt_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {stt_key}"},
                    files={"file": (abs_path.name, data, send_mime)},
                    data={"model": stt_model},
                )
            if response.status_code >= 400:
                last_detail = f"HTTP {response.status_code}: {response.text[:500]}"
                logger.warning("Transcription attempt %d/%d failed: %s", attempt, MAX_RETRIES, last_detail)
            else:
                return response.json()["text"]
        except Exception as e:
            last_detail = f"{type(e).__name__}: {e}"
            logger.warning("Transcription attempt %d/%d failed: %s", attempt, MAX_RETRIES, last_detail)
        if attempt < MAX_RETRIES:
            await asyncio.sleep(attempt ** 2)
    raise RuntimeError(f"Transcription failed after {MAX_RETRIES} attempts — last: {last_detail}")
