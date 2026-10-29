"""Security audit tests — S-1 through S-4, C-2, C-4, C-5, H-2/H-5/H-6/H-7 from backend audit plan."""


import pytest

# WHY: module-level (the file otherwise imports in-function) — @parametrize over
# main._LARGE_JSON_PATHS is evaluated at collection time.

# ── S-3: WebSocket message size limit ────────────────────────────────────────


class TestWsMessageSizeLimit:
    """Collab WS must reject oversized messages."""

    MAX_WS_MESSAGE_SIZE = 1_000_000  # 1MB expected limit

    def test_collab_has_max_message_size_constant(self):
        """The collab WS size limit must exist and be enforced by the channel.

        (MAX_WS_MESSAGE_SIZE is owned by deps; the per-entity route that
        re-exposed it is deleted — plan fewer-layers — so the constant is
        asserted at its owner and the enforcement at the one channel left.)"""
        import inspect

        from routes import collab_project_ws

        from deps import MAX_WS_MESSAGE_SIZE

        assert MAX_WS_MESSAGE_SIZE > 0
        assert "MAX_WS_MESSAGE_SIZE" in inspect.getsource(collab_project_ws)


# ── C-5: SSRF — image URL validation ──────────────────────────────────────────


class TestImageUrlValidation:
    """_validate_image_url must reject non-HTTPS / non-data-URI URLs."""


    def test_https_accepted(self):
        from routes.chat.serializers import _validate_image_url
        _validate_image_url("https://example.com/image.png")


# ── C-4: PATCH/DELETE refs return 404 when ref doesn't exist ───────────────────


@pytest.mark.asyncio
async def test_patch_nonexistent_reference_returns_404(client, admin_user):
    """C-4: PATCH /api/references/{id} must return 404 for missing ref."""
    _, token = admin_user
    resp = await client.patch(
        "/api/references/nonexistent-ref-id",
        json={"title": "ghost"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_nonexistent_reference_returns_404(client, admin_user):
    """C-4: DELETE /api/references/{id} must return 404 for missing ref."""
    _, token = admin_user
    resp = await client.delete(
        "/api/references/nonexistent-ref-id",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


# ── H-6: Content-Length ValueError ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_malformed_content_length_returns_400(client, admin_user):
    """H-6: Non-numeric Content-Length header must return 400, not crash."""
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": "test"},
        cookies={"lore_session": token},
        headers={"Content-Length": "not-a-number"},
    )
    assert resp.status_code == 400


# ── Chat completions 413: structured body with limit_mb ───────────────────────


@pytest.mark.asyncio
async def test_oversized_chat_completion_returns_structured_413(client, admin_user):
    """An over-cap chat-completions body returns 413 with detail AND numeric limit_mb."""
    from config import CHAT_MAX_IMAGE_SIZE_MB
    from main import _LARGE_JSON_MAX_BYTES

    _, token = admin_user
    over_cap = _LARGE_JSON_MAX_BYTES + 1
    resp = await client.post(
        "/api/chat/sessions/abc/completions",
        json={"messages": []},
        cookies={"lore_session": token},
        headers={"Content-Length": str(over_cap)},
    )
    assert resp.status_code == 413
    body = resp.json()
    assert "detail" in body
    assert isinstance(body["limit_mb"], (int, float))
    assert body["limit_mb"] == CHAT_MAX_IMAGE_SIZE_MB


@pytest.mark.asyncio
async def test_oversized_non_chat_413_keeps_bare_shape(client, admin_user):
    """Non-completions over-cap JSON (1MB cap) keeps the old bare-detail 413 shape."""
    _, token = admin_user
    resp = await client.post(
        "/api/chat/sessions",  # no trailing slash → not a completions path
        json={"project_id": "test"},
        cookies={"lore_session": token},
        headers={"Content-Length": str(2 * 1024 * 1024)},
    )
    assert resp.status_code == 413
    assert "limit_mb" not in resp.json()


# ── H-7: security_log passes extra to logger ──────────────────────────────────


def test_security_log_passes_extra():
    """H-7: log_security must pass extra dict to the logger call."""
    import logging

    from security_log import log_security

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord):
            records.append(record)

    handler = Capture()
    logger = logging.getLogger("lore.security")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        log_security("test_event", user_id="u1", ip="1.2.3.4")
        assert len(records) == 1
        assert records[0].security_event == "test_event"  # type: ignore[attr-defined]
        assert records[0].ip == "1.2.3.4"  # type: ignore[attr-defined]
    finally:
        logger.removeHandler(handler)
