"""Tests for GET /api/chat/models — capability fields (vision_models + agent_available).

Pins the response shape of the models endpoint AND the picker's vision source: the
gateway's own per-model capability metadata (`supports_vision` /
`architecture.input_modalities`), not a locally curated model list. These tests
bind the contract for BOTH capability fields so the model picker and the badge
share one derived source of truth. The SEND-TIME gate is NOT here: it reads the
driver's /capability reply (driver.client.agent_capability — plan
collapse-the-editor-harness-layer step 4), pinned in test_driver_client.py.
"""
from __future__ import annotations

import driver.client
import pytest
import routes.chat.models_catalog as comp
from helpers import pin_chat_api


class _FakeResponse:
    """httpx.Response stand-in for the gateway GET /v1/models."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    """httpx.AsyncClient stand-in: serves a fixed /models payload."""

    is_closed = False

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def get(self, url: str, headers: dict | None = None) -> _FakeResponse:  # noqa: ARG002
        return _FakeResponse(self._payload)


async def _ok_capability() -> dict:
    # models_catalog resolves driver.client.agent_capability at call time — the
    # seam to pin.
    return {"available": True}


# ─── /models endpoint ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_models_vision_falls_back_to_input_modalities(
    client, admin_user, monkeypatch, http_pool
):
    """A gateway entry WITHOUT `supports_vision` is still classified from
    `architecture.input_modalities` — the two fields are redundant on the live
    gateway, so losing either one must not silently disable image delivery."""
    pin_chat_api(monkeypatch, url="http://gateway.example")
    monkeypatch.setattr(driver.client, "agent_capability", _ok_capability)
    monkeypatch.setattr(comp, "_gateway_models_cache", None)
    served = [
        {"id": "a/vision", "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "b/text", "architecture": {"input_modalities": ["text"]}},
    ]
    http_pool("models_catalog", _FakeClient({"data": served}))

    _uid, token = admin_user
    resp = await client.get("/api/chat/models", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    assert resp.json()["vision_models"] == ["a/vision"]
