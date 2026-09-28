"""Token-context gauge: the `/models` `context_windows` map, the
`context_usage` frame, relay capture + relay, persistence on normal turn end
only, and session-serializer surfacing.

The turn-time cap source is the DRIVER's own gateway resolution (plugin
caps.ts — plan collapse-the-editor-harness-layer step 4); the Python resolver
suite that threaded it through the payload is deleted with the resolver. The
picker's `context_windows` map (same gateway payload, served by GET /models)
stays pinned here.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import driver.client
import driver.frames
import pytest
import routes.chat.models_catalog as comp
from helpers import pin_chat_api


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    is_closed = False

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def get(self, url: str, headers: dict | None = None) -> _FakeResponse:  # noqa: ARG002
        return _FakeResponse(self._payload)


async def _ok_capability() -> dict:
    # models_catalog resolves driver.client.agent_capability at call time — the
    # seam to pin.
    return {"available": True}


# ─── /models context_windows map ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_models_payload_carries_context_windows_for_models_with_length(
    client, admin_user, monkeypatch, http_pool,
):
    """GET /chat/models includes a `context_windows` map (only for models whose
    gateway entry has a numeric `context_length`); models that omit it are absent
    so the client falls back rather than showing false precision."""
    pin_chat_api(monkeypatch, url="http://gateway.example")
    monkeypatch.setattr(driver.client, "agent_capability", _ok_capability)
    # Reset the module-level gateway cache so this test forces a fresh fetch via
    # the mocked client (test isolation — the TTL cache leaks across tests).
    monkeypatch.setattr(comp, "_gateway_models_cache", None)
    served = [
        {"id": "deepseek/flash", "context_length": 262144},
        {"id": "local/orange/chat", "context_length": 131072},
        {"id": "gemini/chat"},  # omitted
    ]
    http_pool("models_catalog", _FakeClient({"data": served}))

    _uid, token = admin_user
    resp = await client.get("/api/chat/models", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    cw = body["context_windows"]
    assert cw == {"deepseek/flash": 262144, "local/orange/chat": 131072}
    # Models that omit context_length are NOT in the map (client falls back).
    assert "gemini/chat" not in cw


@pytest.mark.asyncio
async def test_models_empty_url_branch_carries_empty_context_windows(
    client, admin_user, monkeypatch,
):
    """The empty-AI_API_URL branch still emits a (empty) `context_windows` map so
    the response shape is identical across branches."""
    pin_chat_api(monkeypatch, url="")
    monkeypatch.setattr(driver.client, "agent_capability", _ok_capability)
    monkeypatch.setattr(comp, "_gateway_models_cache", None)  # test isolation

    _uid, token = admin_user
    resp = await client.get("/api/chat/models", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    assert resp.json()["context_windows"] == {}


# ─── relay capture + relay ──────────────────────────────────────────────────
#
# The typed `context_usage` frame IS live vocabulary — the DRIVER's own frame
# (`_accumulate_frame` captures + relays it verbatim); only the retired
# sse_frames BUILDER is gone (the reducer never used it). Pinned against
# test_driver_frames.py's accumulate test; the persistence seams follow.


def _turn(**persist_overrides):
    return driver.frames._TurnProjection(
        assistant_msg_id="m1",
        sources=None,
        persist_content=persist_overrides.get("persist_content", AsyncMock()),
        persist_sources=persist_overrides.get("persist_sources", AsyncMock()),
        persist_extras=AsyncMock(),
        persist_turn_seq=AsyncMock(),
    )


async def test_relay_context_usage_captures_and_relays():
    """A `context_usage` frame is captured on the projection AND relayed as a
    frontend frame (the gauge reads it live)."""
    r = _turn()
    assert r.context_usage is None  # init null
    frames = await driver.frames._relay_frame(
        r, {"type": "context_usage", "used": 9000, "cap": 262144})
    assert r.context_usage == {"used": 9000, "cap": 262144}
    assert frames == [{"type": "context_usage", "used": 9000, "cap": 262144}]


async def test_relay_context_usage_is_not_terminal():
    """context_usage does NOT set finished (it precedes the terminal done/error)."""
    r = _turn()
    await driver.frames._relay_frame(r, {"type": "context_usage", "used": 1, "cap": 2})
    assert not r.finished


# ─── terminal-close persistence (normal end only — the channel's _close_turn
# semantics; driven here through test_driver_client._drive_turn, a channel in
# miniature whose from-imports read the monkeypatched persist fn) ─────────────


def _turn_end(seq, kind="completed"):
    return {"type": "dsh_event", "kind": "turn/end", "seq": seq,
            "data": {"turn": 1, "reason": {"kind": kind}}}


async def test_normal_end_persists_context_usage_once(monkeypatch):
    """A turn that ends in a graceful `turn/end` (with a prior context_usage
    frame) persists the usage onto chat_sessions exactly once."""
    import driver.persistence
    from test_driver_client import _drive_turn

    calls: list[tuple] = []

    async def _persist(session_id, used):
        calls.append((session_id, used))

    monkeypatch.setattr(driver.persistence, "_persist_context_usage", _persist)

    await _drive_turn(
        [
            {"type": "context_usage", "used": 555, "cap": 262144},
            _turn_end(1),
        ],
        assistant_msg_id="m1", session_id="chat-1",
    )

    assert calls == [("chat-1", 555)]


async def test_error_end_does_not_persist_context_usage(monkeypatch):
    """A turn that ends in an error-reason `turn/end` must NOT stamp a
    misleading usage onto chat_sessions (an abnormal turn has no honest final
    context state)."""
    import driver.persistence
    from test_driver_client import _drive_turn

    calls: list[tuple] = []

    async def _persist(session_id, used):
        calls.append((session_id, used))

    monkeypatch.setattr(driver.persistence, "_persist_context_usage", _persist)

    await _drive_turn(
        [
            {"type": "context_usage", "used": 555, "cap": 262144},
            _turn_end(2, kind="error"),
        ],
        assistant_msg_id="m1", session_id="chat-err",
    )

    assert calls == []


async def test_no_persist_when_no_context_usage_frame(monkeypatch):
    """A turn with NO context_usage frame (e.g. an old driver) persists nothing."""
    import driver.persistence
    from test_driver_client import _chunk, _drive_turn

    calls: list[tuple] = []

    async def _persist(session_id, used):
        calls.append((session_id, used))

    monkeypatch.setattr(driver.persistence, "_persist_context_usage", _persist)

    await _drive_turn(
        [_chunk(1, "hi"), _turn_end(2)],
        assistant_msg_id="m1", session_id="chat-none",
    )

    assert calls == []


# ─── _persist_context_usage ──────────────────────────────────────────────────


async def test_persist_context_usage_writes_session_row(monkeypatch):
    """_persist_context_usage runs an UPDATE on chat_sessions writing `used` only
    (the cap axis was dropped — the client resolves the live cap from /models)."""
    from driver.persistence import _persist_context_usage

    executed: list[tuple] = []

    class _FakeDB:
        async def query(self, sql, params):
            executed.append((sql, params))

    monkeypatch.setattr("driver.persistence.get_db", lambda: _AsyncBox(_FakeDB()))
    await _persist_context_usage("chat-1", 555)
    assert executed, "expected one UPDATE on chat_sessions"
    sql, params = executed[0]
    assert "UPDATE" in sql and "chat_sessions" in sql
    assert "context_tokens_used" in sql
    assert "context_tokens_cap" not in sql
    assert params["used"] == 555
    assert "cap" not in params


class _AsyncBox:
    """Awaitable wrapper so `await get_db()` yields the fake db (get_db is async)."""

    def __init__(self, db) -> None:
        self._db = db

    def __await__(self):
        async def _():
            return self._db
        return _().__await__()


# ─── serializer ──────────────────────────────────────────────────────────────


def test_serialize_session_emits_context_tokens_used():
    """serialize_session surfaces the persisted `used` field when present."""
    from chat_sessions.serialize import serialize_session

    row = {
        "id": "chat_sessions:abc", "title": "t", "user_id": "u1",
        "project_id": "p1", "model": "deepseek/flash",
        "context_document_ids": [],
        "context_tokens_used": 12345,
        "created_at": None,
    }
    out = serialize_session(row)
    assert out["context_tokens_used"] == 12345
    assert "context_tokens_cap" not in out


def test_serialize_session_context_tokens_absent_when_unset():
    """When the DB row lacks the field (pre-migration), the serializer emits null
    for `used` — it does NOT invent a default (the client resolves the cap from /models)."""
    from chat_sessions.serialize import serialize_session

    row = {
        "id": "chat_sessions:abc", "title": "t", "user_id": "u1",
        "project_id": "p1", "model": "deepseek/flash",
        "context_document_ids": [],
        "created_at": None,
    }
    out = serialize_session(row)
    assert out.get("context_tokens_used") is None
    assert "context_tokens_cap" not in out
