"""Unit tests for the extracted transcribe_audio() function."""

import logging
import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


@pytest.fixture
def ogg_file(tmp_path):
    """Create a dummy .ogg audio file."""
    p = tmp_path / "test.ogg"
    p.write_bytes(b"\x00" * 100)
    return p


@pytest.fixture
def webm_file(tmp_path):
    """Create a dummy .webm audio file."""
    p = tmp_path / "test.webm"
    p.write_bytes(b"\x00" * 100)
    return p


def _make_response(text: str, status_code: int = 200, body: str = ""):
    """Build a mock httpx response.

    `body` is what the new transcribe_audio reads as `response.text` on a
    status >= 400 to surface the STT error body in logs / RuntimeError.
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"text": text}
    resp.text = body
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return resp


@pytest.mark.asyncio
async def test_transcribe_audio_returns_text(ogg_file, http_pool):
    mock_response = _make_response("hello world")
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    http_pool("transcription", mock_client)
    from transcription import transcribe_audio
    result = await transcribe_audio(ogg_file)

    assert result == "hello world"
    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    assert "/audio/transcriptions" in call_kwargs.args[0]


@pytest.mark.asyncio
async def test_transcribe_audio_webm_sends_directly(webm_file, http_pool):
    mock_response = _make_response("webm text")
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    http_pool("transcription", mock_client)
    from transcription import transcribe_audio
    result = await transcribe_audio(webm_file)

    assert result == "webm text"
    call_kwargs = mock_client.post.call_args
    files_arg = call_kwargs.kwargs.get("files") or call_kwargs[1].get("files")
    sent_name = files_arg["file"][0]
    sent_mime = files_arg["file"][2]
    assert sent_name == "test.webm"
    assert sent_mime == "audio/webm"


@pytest.mark.asyncio
async def test_transcribe_audio_retries_on_failure(ogg_file, http_pool):
    fail_response = _make_response("", status_code=500)
    ok_response = _make_response("retry success")

    call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return fail_response
        return ok_response

    mock_client = AsyncMock()
    mock_client.post = mock_post

    http_pool("transcription", mock_client)
    with patch("transcription.asyncio.sleep", new_callable=AsyncMock):
        from transcription import transcribe_audio
        result = await transcribe_audio(ogg_file)

    assert result == "retry success"
    assert call_count == 3


@pytest.mark.asyncio
async def test_transcribe_audio_raises_after_max_retries(ogg_file, http_pool):
    fail_response = _make_response("", status_code=500)

    mock_client = AsyncMock()
    mock_client.post.return_value = fail_response

    http_pool("transcription", mock_client)
    with patch("transcription.asyncio.sleep", new_callable=AsyncMock):
        from transcription import transcribe_audio
        with pytest.raises(RuntimeError, match="failed after"):
            await transcribe_audio(ogg_file)


@pytest.mark.asyncio
async def test_transcribe_audio_logs_stt_body_on_error(ogg_file, http_pool, caplog):
    # WHY: a 500 from the STT gateway wraps the provider cause in its body; that
    # body must reach worker logs (via the RuntimeError) instead of the bare status.
    stt_body = '{"error":"Failed to load audio"}'
    fail_response = _make_response("", status_code=500, body=stt_body)

    mock_client = AsyncMock()
    mock_client.post.return_value = fail_response

    http_pool("transcription", mock_client)
    with (
        patch("transcription.asyncio.sleep", new_callable=AsyncMock),
        caplog.at_level(logging.WARNING, logger="transcription"),
    ):
        from transcription import transcribe_audio
        with pytest.raises(RuntimeError, match=re.escape("Failed to load audio")):
            await transcribe_audio(ogg_file)

    assert any(stt_body in r.getMessage() for r in caplog.records), \
        "STT error body must appear in a transcription warning log"


@pytest.mark.asyncio
async def test_transcribe_audio_names_transport_exception(ogg_file, http_pool):
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))

    http_pool("transcription", mock_client)
    with patch("transcription.asyncio.sleep", new_callable=AsyncMock):
        from transcription import transcribe_audio
        with pytest.raises(RuntimeError, match="ConnectError"):
            await transcribe_audio(ogg_file)
