"""Model-gateway catalog — the /v1/models proxy + TTL cache behind GET /models
(the FRONTEND PICKER contract, extracted from completions.py).

Part of the chat-completions system: the HTTP client singleton, the single-
flight TTL cache over the gateway's /v1/models, the picker payload projection
(models list, vision badge set, context_windows map) and the GET /models
route. Turn machinery stays in completions.py.
NOT a gate: the turn-time capability reads (vision, the compaction window,
the output cap) ask the DRIVER (driver.client.agent_capability → the plugin's
own /v1/models resolution); no resolver re-reads this cache to arm a gate.
No SYSTEM marker (internal extraction; the chat-completions entry stays in
completions.py).
"""

import asyncio
import logging

import driver.client
import http_clients
import settings
from fastapi import Depends, HTTPException

from auth import get_current_user
from config import (
    CHAT_MAX_IMAGE_SIZE_MB,
    CHAT_MODELS_TIMEOUT_S,
)
from routes.chat._router import router

logger = logging.getLogger(__name__)

# ─── The pi-ai effort-level vocabulary (the picker↔adapter twin) ───────────────
# pi-ai's canonical level names plus the gateway spellings that map onto them.
#
# WHY this set and its plugin twin (PI_AI_EFFORT_LEVELS + EFFORT_SYNONYMS in
# harness-driver/plugin/src/caps.ts) must move in ONE commit: the picker must
# never offer a level the adapter would refuse — pi-ai fails an undeclared
# level with UNSUPPORTED_REASONING_EFFORT before network I/O — and the adapter
# never declares a level the picker does not offer, or turns start dying at
# the seam. An offer is nameable iff it names a level beyond `off` (the
# off-only rule is applied in _normalize_reasoning_map): pi-ai refuses an
# off-only declaration and a failed catalog resolution takes the WHOLE
# provider route out.
PI_AI_EFFORT_LEVELS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
)
_EFFORT_SYNONYMS = {"none": "off"}


# The /models probe client is the shared pool's "models_catalog" entry
# (SYSTEM: http-clients), built with CHAT_MODELS_TIMEOUT_S — reused across
# chat-panel opens (connection pool / keep-alive survives). Closed on web
# shutdown through the pool.


# ─── Gateway /v1/models cache (the picker snapshot) ──────────────────────────
# The gateway exposes each served model and its real `context_length` — the
# picker's gauge cap source. One TTL-cached fetch serves GET /models; the
# turn-time gates do NOT read this (they ask the driver — module docstring).
_gateway_models_cache: list[dict] | None = None
_gateway_models_cache_at: float = 0.0
# Single-flight lock for the refresh: concurrent callers share ONE gateway GET
# instead of a thundering herd. Created lazily (asyncio.Lock at import time,
# before a loop exists, warns on some Python versions).
_gateway_models_lock: asyncio.Lock | None = None


def _gateway_lock() -> asyncio.Lock:
    global _gateway_models_lock
    if _gateway_models_lock is None:
        _gateway_models_lock = asyncio.Lock()
    return _gateway_models_lock


async def _refresh_gateway_models() -> list[dict]:
    """Fetch the gateway /v1/models raw entries and populate the module cache.

    Raises on any gateway failure (the caller decides 503 vs fallback). Returns the
    entries list (each {id, context_length?, ...})."""
    global _gateway_models_cache, _gateway_models_cache_at
    import time
    api_url = await settings.get("AI_API_URL")
    api_key = await settings.get("AI_API_KEY")
    client = http_clients.get_http_client("models_catalog", timeout=CHAT_MODELS_TIMEOUT_S)
    resp = await client.get(
        f"{api_url}/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    resp.raise_for_status()
    entries = resp.json().get("data", []) or []
    _gateway_models_cache = entries
    _gateway_models_cache_at = time.monotonic()
    return entries


async def _gateway_model_entries() -> list[dict]:
    """TTL-cached gateway /v1/models entries. Returns the cache when fresh,
    otherwise refreshes (and re-raises on a gateway failure so list_models can
    surface its 503 — matching the pre-refactor behavior).

    Single-flight: concurrent callers share one refresh under `_gateway_lock`
    (double-checked — a caller that waited re-reads the freshly-populated cache
    instead of fetching again)."""
    import time
    ttl = await settings.get("CHAT_MODELS_CACHE_TTL_S")
    if _gateway_models_cache is not None and (
        time.monotonic() - _gateway_models_cache_at
    ) < ttl:
        return _gateway_models_cache
    async with _gateway_lock():
        # Re-check after acquiring: another caller may have refreshed while we waited.
        if _gateway_models_cache is not None and (
            time.monotonic() - _gateway_models_cache_at
        ) < ttl:
            return _gateway_models_cache
        return await _refresh_gateway_models()


# ─── Gateway /v1/capabilities cache (the reasoning-effort map) ────────────────
# The gateway's flat per-model reasoning map ({model_id: {supported,
# effort_levels}}) feeds the picker's effort dropdown AND the update_session
# guard (advertised_effort_levels) — one cache, two readers.
# WHY this cache never fails a request: a gateway without the endpoint (any
# third-party OpenAI-compat router), a 404/5xx, or a malformed body is FEATURE
# ABSENCE, not degradation — {} is served, no dropdown renders, no banner. The
# models list staying servable matters more than the effort picker; the turn
# itself works without it (Default = harness-resolved).
_gateway_reasoning_cache: dict | None = None
_gateway_reasoning_cache_at: float = 0.0
# Single-flight for the refresh, mirroring _gateway_models_lock (lazily created
# for the same asyncio-at-import-time reason).
_gateway_reasoning_lock: asyncio.Lock | None = None


def _reasoning_lock() -> asyncio.Lock:
    global _gateway_reasoning_lock
    if _gateway_reasoning_lock is None:
        _gateway_reasoning_lock = asyncio.Lock()
    return _gateway_reasoning_lock


def _normalize_reasoning_map(raw: object) -> dict:
    """Keep only well-shaped entries ({supported: bool, effort_levels: [str]}),
    and only the effort levels pi-ai can name.

    The gateway is ours today, but the URL is config — a third-party
    OpenAI-compat router answering some other shape at /capabilities must not
    push garbage into the guard's legal-values check. The same softness
    applies to individual levels: a level outside pi-ai's vocabulary (and its
    synonym table — see PI_AI_EFFORT_LEVELS above) is dropped here, so the
    picker never offers — and the PATCH guard never accepts — a level the
    adapter would refuse with UNSUPPORTED_REASONING_EFFORT. An offer naming no
    level beyond `off` is no offer either (the plugin twin's off-only rule):
    pi-ai refuses an off-only declaration and a failed catalog resolution
    takes the whole provider route out, so the list flattens to []."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for mid, entry in raw.items():
        if not isinstance(mid, str) or not isinstance(entry, dict):
            continue
        levels = [
            lv for lv in (entry.get("effort_levels") or [])
            if isinstance(lv, str)
            and (lv in PI_AI_EFFORT_LEVELS or lv in _EFFORT_SYNONYMS)
        ]
        if not any(lv in PI_AI_EFFORT_LEVELS and lv != "off" for lv in levels):
            levels = []
        out[mid] = {
            "supported": bool(entry.get("supported")),
            "effort_levels": levels,
        }
    return out


async def _refresh_gateway_reasoning() -> dict:
    """Fetch the gateway /capabilities map and populate the module cache.

    Raises on any transport/status failure (the caller decides — and for this
    cache the decision is always: log, serve {})."""
    global _gateway_reasoning_cache, _gateway_reasoning_cache_at
    import time
    api_url = await settings.get("AI_API_URL")
    api_key = await settings.get("AI_API_KEY")
    client = http_clients.get_http_client("models_catalog", timeout=CHAT_MODELS_TIMEOUT_S)
    resp = await client.get(
        f"{api_url}/capabilities",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    resp.raise_for_status()
    mapping = _normalize_reasoning_map(resp.json())
    _gateway_reasoning_cache = mapping
    _gateway_reasoning_cache_at = time.monotonic()
    return mapping


async def gateway_reasoning_map() -> dict:
    """TTL-cached gateway /capabilities reasoning map; {} on ANY failure.

    Same TTL (CHAT_MODELS_CACHE_TTL_S) and single-flight shape as the models
    cache beside it. A failed refresh populates nothing, so the next caller
    retries after the in-flight attempt — bounded by lock serialization."""
    import time
    ttl = await settings.get("CHAT_MODELS_CACHE_TTL_S")
    if _gateway_reasoning_cache is not None and (
        time.monotonic() - _gateway_reasoning_cache_at
    ) < ttl:
        return _gateway_reasoning_cache
    async with _reasoning_lock():
        if _gateway_reasoning_cache is not None and (
            time.monotonic() - _gateway_reasoning_cache_at
        ) < ttl:
            return _gateway_reasoning_cache
        try:
            return await _refresh_gateway_reasoning()
        except Exception as e:
            logger.warning("capabilities fetch failed (serving {}): %s", e)
            return {}


async def advertised_effort_levels(model_id: str) -> list[str] | None:
    """The model's advertised reasoning-effort levels, or None when the model
    advertises none (absent from the map, or supported=false).

    The update_session guard reads this: None and [] carry the same verdict —
    no non-null effort is legal — but None keeps the 400 honest about the
    model being unknown rather than merely non-reasoning."""
    mapping = await gateway_reasoning_map()
    entry = mapping.get(model_id)
    if not isinstance(entry, dict) or not entry.get("supported"):
        return None
    return list(entry.get("effort_levels") or [])


def _entry_supports_vision(entry: dict) -> bool:
    """True when a gateway /v1/models entry declares image input.

    Reads the gateway's own capability metadata — the explicit `supports_vision`
    flag when present, else `architecture.input_modalities`. Both are served on
    every live model and agree; accepting either keeps the badge working if one
    is dropped from the payload. (The plugin's caps.ts carries the twin
    predicate for the turn-time gate.)

    INVARIANT: an entry declaring NEITHER field is non-vision. Why: a model that
    rejects image parts fails the whole turn with an opaque 400, so the safe
    failure direction is to strip the image and warn (visible) rather than send it.
    """
    flag = entry.get("supports_vision")
    if isinstance(flag, bool):
        return flag
    modalities = (entry.get("architecture") or {}).get("input_modalities") or []
    return "image" in modalities


def _context_windows_map(entries: list[dict]) -> dict[str, int]:
    """Build the {model_id: context_length} map for models the gateway reported a
    numeric window for. Models that omit it are ABSENT (the client falls back rather
    than showing false precision)."""
    out: dict[str, int] = {}
    for m in entries:
        mid = m.get("id")
        cl = m.get("context_length")
        if mid and cl:
            try:
                out[mid] = int(cl)
            except (TypeError, ValueError):
                continue
    return out


# ─── Models endpoint ──────────────────────────────────────────────────────────


def _models_payload(
    entries: list[dict],
    cap: dict,
    reasoning: dict | None = None,
    default_model: str | None = None,
) -> dict:
    """Build the GET /models response body from the raw gateway entries.

    Capability fields (vision_models + agent_available + the reasoning map) are
    merged here so BOTH the success and the empty-AI_API_URL branches stay
    identical in shape.

    Every per-model field the client gets — the id, the vision badge, the context
    window, the reasoning-effort levels — is projected from ONE gateway payload
    here, so the picker cannot disagree with what the provider serves. (The
    turn-time gates ask the DRIVER, which resolves the same roster —
    driver.client.agent_capability; picker and gates agree because they read
    the same gateway, not because they share code.)
    `default_model` is omitted entirely when None (empty-gateway branch), matching
    the historical response shape.

    `context_windows` carries each served model's real `context_length` (only
    models the gateway reported a window for); the frontend gauge reads it as the
    cap source (the turn's cap is the driver's own resolution).

    `reasoning` is the gateway's per-model {supported, effort_levels} map —
    the effort dropdown's single source. {} when the gateway has no
    /capabilities (feature absence — see gateway_reasoning_map).
    """
    models = [m["id"] for m in entries if m.get("id")]
    body = {
        "models": sorted(models),
        "vision_models": sorted(
            m["id"] for m in entries if m.get("id") and _entry_supports_vision(m)
        ),
        "max_attachment_mb": CHAT_MAX_IMAGE_SIZE_MB,
        "agent_available": cap["available"],
        "agent_unavailable_reason": cap.get("reason"),
        "context_windows": _context_windows_map(entries),
        "reasoning": dict(reasoning or {}),
    }
    if default_model is not None:
        body["default_model"] = default_model
    return body


async def _capability_snapshot() -> dict:
    """THE line's capability — configured-or-not, NO probe (a down driver
    surfaces at turn time).
    Read through the OWNING module's attribute — the vision tests patch
    driver.client.agent_capability and a frozen binding here would make the
    patch succeed while inert."""
    return await driver.client.agent_capability()


@router.get("/models")
async def list_models(_user: dict = Depends(get_current_user)):
    """Proxy GET /v1/models from the AI API. Returns model ID list + capability flags.

    Reports Agent-line availability and the vision-capable model set
    so the frontend picker can badge vision models instead of erroring at send time.
    Both capability fields are merged into the success AND empty-AI_API_URL bodies.
    The 503-on-gateway-down path stays: when the whole gateway is down the flags are
    irrelevant. Also returns `context_windows` (per-model real context length)
    from the same cached gateway fetch.

    Freshness: the served model list, default_model, and context_windows come from a
    CHAT_MODELS_CACHE_TTL_S (60s) TTL cache — a freshly-added gateway model (or a
    removed one) can lag by up to that bound, and a chat-panel open reads the
    cached snapshot rather than a fresh gateway fetch. The set of served models
    changes on the order of weeks, so the staleness is accepted over a per-open
    gateway RTT. The `reasoning` map rides its own cache with the same TTL and
    the same accepted lag.
    """
    api_url = await settings.get("AI_API_URL")
    if not api_url:
        cap = await _capability_snapshot()
        return _models_payload([], cap)
    try:
        # Run the gateway fetches and the capability check concurrently — they're
        # independent. agent_capability is a config read (no probe, cannot
        # raise) and gateway_reasoning_map serves {} on failure, so only a
        # /models gateway failure → 503.
        entries, cap, reasoning = await asyncio.gather(
            _gateway_model_entries(),
            _capability_snapshot(),
            gateway_reasoning_map(),
        )
        return _models_payload(
            entries, cap, reasoning=reasoning,
            default_model=await settings.get("CHAT_MODEL"),
        )
    except Exception as e:
        logger.warning("Failed to fetch models: %s", e)
        raise HTTPException(status_code=503, detail="AI service unavailable")
