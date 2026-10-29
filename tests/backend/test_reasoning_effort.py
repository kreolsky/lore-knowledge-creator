"""Reasoning-effort selector — backend pieces (plan reasoning-effort-selector step 2).

# SYSTEM: reasoning-effort tests — the /api/chat/models `reasoning` map, the
# per-session reasoning_effort column (create/PATCH + the update guard), and
# the turn-payload threading.

The map is fed by the gateway's GET /v1/capabilities (same key as /v1/models,
auth parity), TTL-cached beside the models cache. A gateway WITHOUT the
endpoint (any third-party OpenAI-compat router) is feature absence, not
degradation: the map serves {} and the picker renders no dropdown — pinned
here so the failure direction can never become a 503 or a banner.

Normalization also filters each entry's levels down to the pi-ai vocabulary
(PI_AI_EFFORT_LEVELS in models_catalog — the twin of the plugin's translation
table in harness-driver/plugin/src/caps.ts): the picker never offers, and the
PATCH guard never accepts, a level the harness adapter would refuse with
UNSUPPORTED_REASONING_EFFORT before network I/O.

The per-session column follows the system_prompt_id pattern (option<string>):
null = Default (no reasoning_effort on the wire, the provider's default
applies), an explicit value is validated against the TARGET model's
advertised list at PATCH time — 400 outside it, null always legal (the
model-change reset rides this).
"""
from __future__ import annotations

import time

import driver.client
import pytest
import routes.chat.models_catalog as comp
from helpers import pin_chat_api

# ─── fakes: one gateway client serving /models AND /capabilities ─────────────


class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"HTTP {self.status}", request=None, response=None
            )


class _RouterClient:
    """httpx.AsyncClient stand-in: serves /models and /capabilities per URL."""

    is_closed = False

    def __init__(self, models: dict, caps: dict | None = None, caps_status: int = 200):
        self._models = models
        self._caps = caps if caps is not None else {}
        self._caps_status = caps_status

    async def get(self, url: str, headers: dict | None = None) -> _Resp:  # noqa: ARG002
        if url.endswith("/capabilities"):
            return _Resp(self._caps, status=self._caps_status)
        return _Resp(self._models)


async def _ok_capability() -> dict:
    return {"available": True}


def _reset_gateway_caches(monkeypatch) -> None:
    """Force fresh fetches for BOTH the models cache and the reasoning cache."""
    monkeypatch.setattr(comp, "_gateway_models_cache", None)
    monkeypatch.setattr(comp, "_gateway_reasoning_cache", None)


def _seed_reasoning_cache(monkeypatch, mapping: dict) -> None:
    """Serve a fixed capabilities map from the TTL cache (no HTTP)."""
    monkeypatch.setattr(comp, "_gateway_reasoning_cache", mapping)
    monkeypatch.setattr(comp, "_gateway_reasoning_cache_at", time.monotonic())


# The live derivation shape (router model_service.capabilities): flat
# {model_id: {supported, effort_levels}} filtered exactly like /v1/models.
_CAPS = {
    "deepseek/flash": {"supported": True, "effort_levels": ["low", "high", "max"]},
    "local/orange/chat": {"supported": False, "effort_levels": []},
}

_SERVED = [
    {"id": "deepseek/flash", "supports_vision": False},
    {"id": "local/orange/chat", "supports_vision": True},
]


# ─── GET /api/chat/models — the reasoning map ─────────────────────────────────


@pytest.mark.asyncio
async def test_models_capabilities_unreachable_serves_empty_reasoning(
    client, admin_user, monkeypatch, http_pool
):
    """A gateway without /v1/capabilities (404) is feature absence, never a 503:
    the models list still serves, with `reasoning: {}`."""
    pin_chat_api(monkeypatch, url="http://gateway.example")
    monkeypatch.setattr(driver.client, "agent_capability", _ok_capability)
    _reset_gateway_caches(monkeypatch)
    http_pool(
        "models_catalog",
        _RouterClient({"data": _SERVED}, caps_status=404),
    )

    _uid, token = admin_user
    resp = await client.get("/api/chat/models", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["models"] == ["deepseek/flash", "local/orange/chat"]
    assert body["reasoning"] == {}


@pytest.mark.asyncio
async def test_models_drops_malformed_capabilities_entries(
    client, admin_user, monkeypatch, http_pool
):
    """Non-dict entries and non-string levels never reach the payload — the map
    is served normalized ({supported: bool, effort_levels: [str]})."""
    pin_chat_api(monkeypatch, url="http://gateway.example")
    monkeypatch.setattr(driver.client, "agent_capability", _ok_capability)
    _reset_gateway_caches(monkeypatch)
    http_pool(
        "models_catalog",
        _RouterClient(
            {"data": _SERVED},
            caps={
                "deepseek/flash": {"supported": True, "effort_levels": ["low", 7]},
                "garbage": "not-a-dict",
            },
        ),
    )

    _uid, token = admin_user
    resp = await client.get("/api/chat/models", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    assert resp.json()["reasoning"] == {
        "deepseek/flash": {"supported": True, "effort_levels": ["low"]}
    }


# ─── the pi-ai nameability filter (twin of the plugin's translation table) ────


def test_normalize_off_only_offer_is_no_offer():
    """Twin of the plugin's off-only rule: pi-ai refuses a declaration that
    offers nothing beyond `off` (catalog.ts:688), and a failed catalog
    resolution takes the WHOLE provider route out (adapter.ts:135) — so an
    offer with no non-off level is no offer. The off-synonym (and a literal
    `off`) flatten to []; an offer with a level beyond off keeps them."""
    raw = {
        "m/none-only": {"supported": True, "effort_levels": ["none"]},
        "m/off-literal": {"supported": True, "effort_levels": ["off"]},
        "m/mixed": {"supported": True, "effort_levels": ["none", "ultra"]},
        "m/ok": {"supported": True, "effort_levels": ["none", "low"]},
    }
    assert comp._normalize_reasoning_map(raw) == {
        "m/none-only": {"supported": True, "effort_levels": []},
        "m/off-literal": {"supported": True, "effort_levels": []},
        "m/mixed": {"supported": True, "effort_levels": []},
        "m/ok": {"supported": True, "effort_levels": ["none", "low"]},
    }


@pytest.mark.asyncio
async def test_patch_rejects_the_off_synonym_of_an_off_only_offer(
    client, admin_user, project_with_doc, monkeypatch
):
    """A none-only model must not pin `none`: the plugin declares nothing for
    it (off-only guard), so the turn would die in dsh — the row may not carry
    a level no declaration backs."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id, "model": "deepseek/flash",
    })).json()["session_id"]
    _seed_reasoning_cache(
        monkeypatch,
        comp._normalize_reasoning_map(
            {"deepseek/flash": {"supported": True, "effort_levels": ["none"]}}
        ),
    )

    resp = await client.patch(
        f"/api/chat/sessions/{sid}",
        json={"reasoning_effort": "none"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400, resp.text


# ─── session column: create ──────────────────────────────────────────────────


async def _post_session(client, token, body):
    return await client.post(
        "/api/chat/sessions", json=body, cookies={"lore_session": token}
    )


# ─── session column: PATCH + the update guard ─────────────────────────────────


def _seed_caps(monkeypatch):
    _seed_reasoning_cache(monkeypatch, _CAPS)


@pytest.mark.asyncio
async def test_patch_reasoning_effort_outside_list_rejected(
    client, admin_user, project_with_doc, monkeypatch
):
    """A value outside the model's advertised list is a 400 naming the
    constraint — never a row that dies upstream at turn time."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id, "model": "deepseek/flash",
    })).json()["session_id"]
    _seed_caps(monkeypatch)

    resp = await client.patch(
        f"/api/chat/sessions/{sid}",
        json={"reasoning_effort": "medium"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400, resp.text
    assert "medium" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_patch_reasoning_effort_null_always_legal_and_clears(
    client, admin_user, project_with_doc, monkeypatch
):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id,
        "model": "deepseek/flash", "reasoning_effort": "high",
    })).json()["session_id"]
    _seed_caps(monkeypatch)

    resp = await client.patch(
        f"/api/chat/sessions/{sid}",
        json={"reasoning_effort": None},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reasoning_effort"] is None


@pytest.mark.asyncio
async def test_patch_effort_validated_against_patched_target_model(
    client, admin_user, project_with_doc, monkeypatch
):
    """When model + effort land in ONE PATCH, the guard validates against the
    TARGET (body.model) — a level legal for the old model but not the new one
    is rejected; legal for the new one it lands."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = (await _post_session(client, token, {
        "project_id": pid, "document_id": doc_id, "model": "deepseek/flash",
    })).json()["session_id"]
    _seed_caps(monkeypatch)

    resp = await client.patch(
        f"/api/chat/sessions/{sid}",
        json={"model": "local/orange/chat", "reasoning_effort": "high"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400, resp.text

    resp = await client.patch(
        f"/api/chat/sessions/{sid}",
        json={"model": "deepseek/flash", "reasoning_effort": "max"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reasoning_effort"] == "max"
