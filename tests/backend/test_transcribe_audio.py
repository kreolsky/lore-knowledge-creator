"""Unit tests for the extracted transcribe_audio() function."""

from unittest.mock import AsyncMock, MagicMock

import pytest


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
