"""Voice transcription endpoint for chat input."""
# SYSTEM: chat-transcription — audio-to-text for chat input (no Reference side effects)

import logging
import tempfile
from pathlib import Path

import settings
from fastapi import Depends, File, HTTPException, UploadFile
from rate_limit import check_transcribe_rate_limit
from transcription import transcribe_audio

from auth import get_current_user
from config import AUDIO_MIMES
from routes.chat._router import router

logger = logging.getLogger(__name__)


@router.post("/transcribe")
async def chat_transcribe(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """Transcribe audio to text for chat input. No Reference creation.

    # INVARIANT: enforce a per-user request-count limit (5/60s) BEFORE the expensive STT
    # call. Why: STT is synchronous and minutes-long; STT_CONCURRENCY bounds in-flight jobs
    # but not requests/min from one user, so without this one user can exhaust the shared
    # worker pool for everyone.
    """
    if not await check_transcribe_rate_limit(user["user_id"]):
        raise HTTPException(status_code=429, detail="Too many transcription requests. Try again later.")

    if file.content_type not in AUDIO_MIMES:
        raise HTTPException(status_code=400, detail="Unsupported audio format")

    # H-4: Enforce file size limit before processing
    content = await file.read()
    max_audio_mb = await settings.get("MAX_AUDIO_SIZE_MB")
    if len(content) > max_audio_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Audio file exceeds {max_audio_mb}MB limit")

    suffix = Path(file.filename or "audio.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        text = await transcribe_audio(tmp_path)
        return {"text": text}
    finally:
        tmp_path.unlink(missing_ok=True)
