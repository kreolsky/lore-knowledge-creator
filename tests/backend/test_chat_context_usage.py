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

import driver.client
import driver.frames
import pytest
import routes.chat.models_catalog as comp
from helpers import pin_chat_api


async def _ok_capability() -> dict:
    # models_catalog resolves driver.client.agent_capability at call time — the
    # seam to pin.
    return {"available": True}


# ─── /models context_windows map ──────────────────────────────────────────────


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
