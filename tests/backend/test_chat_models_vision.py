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


# The live dev gateway shape as of the capability rollout: every served model
# carries BOTH `supports_vision` and `architecture.input_modalities`, and they
# agree. Tests below serve entries in this shape (plus degraded variants).
_SERVED = [
    {
        "id": "deepseek/flash",
        "supports_vision": False,
        "architecture": {"input_modalities": ["text"]},
    },
    {
        "id": "local/orange/chat",
        "supports_vision": True,
        "architecture": {"input_modalities": ["text", "image"]},
    },
    {
        "id": "gemini/pro",
        "supports_vision": True,
        "architecture": {"input_modalities": ["text", "image", "video", "file", "audio"]},
    },
]


# ─── /models endpoint ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_models_empty_chat_api_url_returns_empty_capability_fields(
    client, admin_user, monkeypatch
):
    """Empty-AI_API_URL early-return branch carries `vision_models: []` and an
    `agent_available` flag (mirrors the agent-availability audit fix shape)."""
    pin_chat_api(monkeypatch, url="")
    # agent_capability still runs in this branch — stub it so no real probe fires.
    monkeypatch.setattr(driver.client, "agent_capability", _ok_capability)

    _uid, token = admin_user
    resp = await client.get("/api/chat/models", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["models"] == []
    assert body["vision_models"] == []
    assert "agent_available" in body


@pytest.mark.asyncio
async def test_models_vision_set_derived_from_gateway_capability(
    client, admin_user, monkeypatch, http_pool
):
    """`vision_models` is exactly the sorted subset of served models the GATEWAY
    reports as vision-capable. The expectation is DERIVED by walking the served
    entries through the same predicate the endpoint uses — never a literal, which
    would drift with the gateway's model roster."""
    pin_chat_api(monkeypatch, url="http://gateway.example")
    monkeypatch.setattr(driver.client, "agent_capability", _ok_capability)
    # Reset the module-level gateway cache (the picker's TTL cache) so this
    # test forces a fresh fetch via the mocked client — test isolation against
    # other models tests.
    monkeypatch.setattr(comp, "_gateway_models_cache", None)
    http_pool("models_catalog", _FakeClient({"data": _SERVED}))

    _uid, token = admin_user
    resp = await client.get("/api/chat/models", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    models = body["models"]
    vision = body["vision_models"]
    expected = sorted(e["id"] for e in _SERVED if comp._entry_supports_vision(e))
    assert vision == expected
    # Two-way contract: no served model is silently missing from the partition.
    assert set(vision) <= set(models)
    assert vision == ["gemini/pro", "local/orange/chat"]


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


@pytest.mark.asyncio
async def test_models_bare_entry_is_not_vision(client, admin_user, monkeypatch, http_pool):
    """An entry carrying NEITHER capability field is treated as non-vision.

    Failure direction is deliberate: unknown → images stripped with a visible
    warning (safe), never a 400 from a model that rejects image parts (opaque)."""
    pin_chat_api(monkeypatch, url="http://gateway.example")
    monkeypatch.setattr(driver.client, "agent_capability", _ok_capability)
    monkeypatch.setattr(comp, "_gateway_models_cache", None)
    http_pool("models_catalog", _FakeClient({"data": [{"id": "bare/model"}]}))

    _uid, token = admin_user
    resp = await client.get("/api/chat/models", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    assert resp.json()["vision_models"] == []
