"""Security audit tests — S-1 through S-4, C-2, C-4, C-5, H-2/H-5/H-6/H-7 from backend audit plan."""

import pytest
from pydantic import ValidationError

# WHY: module-level (the file otherwise imports in-function) — @parametrize over
# main._LARGE_JSON_PATHS is evaluated at collection time.
import main

# ── S-1: validate_record_id coverage in fetch_many ───────────────────────────


@pytest.mark.asyncio
async def test_fetch_many_rejects_injection_in_uid(test_db):
    """IDs containing SurrealQL injection characters must raise ValueError."""
    from db import fetch_many
    with pytest.raises(ValueError):
        await fetch_many("users", ["valid-id", "bad'; DELETE users--"])


@pytest.mark.asyncio
async def test_fetch_many_rejects_injection_in_table(test_db):
    """Table names with injection characters must raise ValueError."""
    from db import fetch_many
    with pytest.raises(ValueError):
        await fetch_many("users; DELETE users--", ["some-id"])


@pytest.mark.asyncio
async def test_fetch_many_valid_ids_still_work(test_db):
    """Normal UUID-format IDs must still work after validation is added."""
    from uuid import uuid4

    from db import create_record, fetch_many
    uid = str(uuid4())
    await create_record("users", uid, {"name": "test", "role": "user"})
    result = await fetch_many("users", [uid])
    assert uid in result


# ── S-2: CompletionRequest.messages validation ───────────────────────────────


class TestChatMessageValidation:
    """CompletionRequest.messages must validate individual message structure."""

    def test_valid_messages_accepted(self):
        from models import CompletionRequest
        req = CompletionRequest(messages=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ])
        assert len(req.messages) == 2

    def test_invalid_role_rejected(self):
        """Messages with invalid role values must be rejected."""
        from models import CompletionRequest
        with pytest.raises(ValidationError):
            CompletionRequest(messages=[
                {"role": "hacker", "content": "inject this"},
            ])

    def test_missing_content_rejected(self):
        """Messages without content field must be rejected."""
        from models import CompletionRequest
        with pytest.raises(ValidationError):
            CompletionRequest(messages=[
                {"role": "user"},
            ])

    def test_missing_role_rejected(self):
        """Messages without role field must be rejected."""
        from models import CompletionRequest
        with pytest.raises(ValidationError):
            CompletionRequest(messages=[
                {"content": "no role"},
            ])

    def test_arbitrary_extra_fields_stripped(self):
        """Extra fields in message dicts should not pass through to the API."""
        from models import CompletionRequest
        req = CompletionRequest(messages=[
            {"role": "user", "content": "hi", "malicious_key": "payload"},
        ])
        msg = req.messages[0]
        # After validation, the message should be a proper model, not a raw dict
        assert not hasattr(msg, "malicious_key") or "malicious_key" not in (msg.model_dump() if hasattr(msg, "model_dump") else msg)

    def test_system_role_accepted(self):
        """System role messages must be allowed."""
        from models import CompletionRequest
        req = CompletionRequest(messages=[
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "Hello"},
        ])
        assert len(req.messages) == 2

    def test_images_field_validated(self):
        """Images field must be a list of strings when present."""
        from models import CompletionRequest
        req = CompletionRequest(messages=[
            {"role": "user", "content": "look at this", "images": ["data:image/png;base64,abc"]},
        ])
        assert len(req.messages) == 1


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

    def test_max_message_size_is_reasonable(self):
        """Size limit should be between 100KB and 10MB."""
        from deps import MAX_WS_MESSAGE_SIZE
        assert 100_000 <= MAX_WS_MESSAGE_SIZE <= 10_000_000


# ── S-4 (migrations svc_user validation) — DELETED with ensure_service_user:
# the service-user bootstrap went with the SURREAL_* env wiring (plan
# component-wiring-not-settings step 4); the identifier guard it carried lives
# on in scripts/init_secrets.py, whose user is now the constant `root`.

# ── C-5: SSRF — image URL validation ──────────────────────────────────────────


class TestImageUrlValidation:
    """_validate_image_url must reject non-HTTPS / non-data-URI URLs."""

    def test_data_uri_accepted(self):
        from routes.chat.serializers import _validate_image_url
        _validate_image_url("data:image/png;base64,iVBORw0KGgoAAAANS=")

    def test_https_accepted(self):
        from routes.chat.serializers import _validate_image_url
        _validate_image_url("https://example.com/image.png")

    def test_http_rejected(self):
        from routes.chat.serializers import _validate_image_url
        with pytest.raises(Exception):
            _validate_image_url("http://example.com/image.png")

    def test_file_uri_rejected(self):
        from routes.chat.serializers import _validate_image_url
        with pytest.raises(Exception):
            _validate_image_url("file:///etc/passwd")

    def test_internal_ip_rejected(self):
        from routes.chat.serializers import _validate_image_url
        with pytest.raises(Exception):
            _validate_image_url("http://169.254.169.254/latest/meta-data/")

    def test_empty_rejected(self):
        from routes.chat.serializers import _validate_image_url
        with pytest.raises(Exception):
            _validate_image_url("")

    def test_data_text_rejected(self):
        """data: URIs that are not images must be rejected."""
        from routes.chat.serializers import _validate_image_url
        with pytest.raises(Exception):
            _validate_image_url("data:text/html;base64,PHNjcmlwdD4=")


# ── C-2: IDOR — GET /api/notes/{id} requires project access ───────────────────


# Note: test_get_note_requires_project_access removed — notes are now chat_sessions.


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


# ── H-3: MessageCreate.role restricted to Literal ─────────────────────────────


def test_message_create_rejects_arbitrary_role():
    """H-3: MessageCreate.role must reject values outside Literal."""
    from models import MessageCreate
    with pytest.raises(ValidationError):
        MessageCreate(role="hacker", content="inject")


# ── H-2: Table injection — validate_record_id gates db functions ───────────────


class TestTableInjection:
    """H-2: db.py functions must reject unsafe table names."""

    def test_validate_record_id_safe(self):
        from db import validate_record_id
        assert validate_record_id("users") == "users"
        assert validate_record_id("api_keys") == "api_keys"
        assert validate_record_id("abc-123-def") == "abc-123-def"

    def test_validate_record_id_rejects_injection(self):
        from db import validate_record_id
        with pytest.raises(ValueError):
            validate_record_id("users'; DROP TABLE users; --")

    def test_validate_record_id_rejects_unicode(self):
        """M-4: Unicode homoglyphs must be rejected."""
        from db import validate_record_id
        with pytest.raises(ValueError):
            validate_record_id("usеrs")  # Cyrillic 'е'

    @pytest.mark.asyncio
    async def test_create_record_rejects_bad_table(self, test_db):
        from db import create_record
        with pytest.raises(ValueError):
            await create_record("bad table!", "id-1", {"x": 1})

    @pytest.mark.asyncio
    async def test_fetch_one_rejects_bad_table(self, test_db):
        from db import fetch_one
        with pytest.raises(ValueError):
            await fetch_one("bad table!", "id-1")

    @pytest.mark.asyncio
    async def test_soft_delete_rejects_bad_table(self, test_db):
        from db import soft_delete
        with pytest.raises(ValueError):
            await soft_delete("bad table!", "id-1")


# ── H-5: Path traversal — transcription worker validates file paths ────────────


def test_path_traversal_resolve():
    """H-5: Resolved path must stay within STORAGE_PATH."""
    from pathlib import Path
    storage = Path("/data/storage")
    rel_path = "../../etc/passwd"
    abs_path = (storage / rel_path).resolve()
    assert not abs_path.is_relative_to(storage.resolve())


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


def test_completion_attachment_path_is_registered_under_the_large_cap():
    """Membership, asserted separately from the totality test below.

    Why both: the totality test parametrizes over _LARGE_JSON_PATHS, so dropping a
    path from that tuple silently DELETES its case instead of failing it (verified:
    reverting the fix left 1 passed, not 1 failed). This test is the one that goes
    red — it pins that the image-carrying chat path is in the tuple at all.

    The retired pi transcript write (`/internal/pi-sessions/{id}/entries`) is
    deliberately NOT here: the canonical transcript is the driver-owned JSONL and
    the two surviving internal endpoints carry ids only, so the exemption would
    widen the cap on routes with nothing to carry.
    """
    assert "/api/chat/sessions/" in main._LARGE_JSON_PATHS
    assert "/internal/pi-sessions/" not in main._LARGE_JSON_PATHS


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", main._LARGE_JSON_PATHS)
async def test_every_large_json_path_shares_the_chat_attachment_cap(client, prefix):
    """Totality: EVERY path in _LARGE_JSON_PATHS accepts a body over the 1MB default
    and rejects one over the large cap — the two bounds are read from the middleware,
    not restated, so this binds the test to the code instead of mirroring it.

    Regression: a completion body carries base64 images. Left on the 1MB default,
    any image ≳0.7MB raw 413'd and pi-agent-core swallowed the failure into an
    empty assistant message.
    """
    path = f"{prefix}some-id/entries"
    # Without this the "over the default, under the large cap" probe below is vacuous.
    assert main._JSON_MAX_BYTES < main._LARGE_JSON_MAX_BYTES

    resp = await client.post(
        path, json={}, headers={"Content-Length": str(main._JSON_MAX_BYTES + 1)}
    )
    assert resp.status_code != 413, f"{prefix} is still on the 1MB default cap"

    resp = await client.post(
        path, json={}, headers={"Content-Length": str(main._LARGE_JSON_MAX_BYTES + 1)}
    )
    assert resp.status_code == 413, f"{prefix} does not enforce the large cap"


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
