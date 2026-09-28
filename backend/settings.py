"""Instance settings — DB overrides over env defaults, resolved at call time.

# ARCH: three layers, one reader — a value is default → env → DB override, and
the read path for a live key is `await settings.get(key)`: the instance_settings
row if present, else config.<KEY>, walking the key's registry `fallback` links
(the config.py import-time folds) so a DB override on a BASE key reaches every
dependent reader. config.py is UNTOUCHED — it stays the default⊕env layer, and
tests that monkeypatch config attrs keep working because the fallback resolves
through config at CALL time, not import time.

The override cache is process-local, loaded from instance_settings on first
access and dropped on SETTINGS_EVENT — delivered locally by the PUT route and
cross-process over the Redis backplane (the worker subscribes in
jobs.worker._on_worker_startup; the web process subscribes in main.py's
lifespan). No polling, no TTL: an event only nudges a reload.
"""
# SYSTEM: instance-settings — DB overrides over env over defaults; admin settings surface

from __future__ import annotations

import json
import logging
import os

from settings_registry import find

import config

logger = logging.getLogger(__name__)

SETTINGS_EVENT = "instance_settings_changed"

# key → JSON-encoded override value. None = not loaded yet (next get() loads).
# INVARIANT: after SETTINGS_EVENT the cache is None again in EVERY process that
# subscribed (web via main.py lifespan, workers via _on_worker_startup).
# Why: a live override must reach the next request/job everywhere — a stale
# process-local cache would silently keep serving the previous value with no
# signal anywhere.
_cache: dict[str, str] | None = None


def drop_cache(**_: object) -> None:
    """Drop the override cache (event_bus subscriber — kwargs-tolerant)."""
    global _cache
    _cache = None


def bootstrap_value(key: str):
    """The env⊕default leg ONLY, synchronously — for import-time seams.

    A DB override cannot apply at import time (no loop, no DB yet); a seam that
    must bind at import (e.g. main.py's starlette kwdefault patch) reads this
    and accepts that overrides land on the call-time readers instead.
    """
    return getattr(config, key)


async def load_overrides() -> dict[str, str]:
    """All override rows (key → JSON-encoded value) — and the ONE writer of
    the process cache: the admin route reads it for the source chips and then
    resolves effective values through `get`, so the read PRIMES `_cache` and
    `get`'s lazy fill never becomes a second, identical DB read."""
    global _cache
    from db import get_db

    db = await get_db()
    rows = await db.query("SELECT key, value FROM instance_settings")
    _cache = {
        row["key"]: row["value"]
        for row in (rows or [])
        if row.get("value") is not None
    }
    return _cache


def _decode(key: str, raw: str):
    entry = find(key)
    return entry.decode(raw) if entry is not None else json.loads(raw)


def _chain(key: str) -> tuple[str, ...]:
    """(key, *entry.fallback) — the walk order, declared once in the registry."""
    entry = find(key)
    return (key, *entry.fallback) if entry is not None else (key,)


def _resolve(chain: tuple[str, ...]):
    """Walk a fallback chain left-to-right, override-aware, in config.py's own
    fold order: the first key with a DB override row wins; else the first key
    whose OWN env var is set resolves through config; else the LAST key
    resolves row → config."""
    for key in chain[:-1]:
        raw = _cache.get(key)
        if raw is not None:
            return _decode(key, raw)
        entry = find(key)
        if entry is not None and entry.env and entry.env in os.environ:
            return getattr(config, key)
    last = chain[-1]
    raw = _cache.get(last)
    return _decode(last, raw) if raw is not None else getattr(config, last)


async def get(key: str):
    """Resolve `key`: DB override if present, else the config value (env⊕default).

    WHY the fallback walk and not a plain config read: config.py folds derived
    defaults at import (e.g. TURN_PROGRESS_GRACE_S ← TURN_TIMEOUT_S,
    STT_API_URL ← AI_API_URL), so a DB override on a BASE key
    can never surface through the dependent's config value — the dependent's
    import-time folding already baked the base's ENV value in. The chain is
    the registry's `fallback` fact (declared once per dependent key); readers
    just `get` the dependent.
    """
    if _cache is None:
        await load_overrides()
    return _resolve(_chain(key))


async def get_all(keys: list[str]) -> dict:
    """Multi-key `get` — one cache load, same per-key resolution rule."""
    if _cache is None:
        await load_overrides()
    return {key: _resolve(_chain(key)) for key in keys}


# Load-bearing import side effect (same posture as embeddings' bus subscription
# in jobs/worker.py): merely importing settings registers this process's cache
# drop. Combined with subscribe_backplane_events() — which subscribes a channel
# per locally-registered event type — the process then receives other
# processes' PUTs. Without the registration the backplane leg silently skips
# the channel (the subscribe loop walks only registered types).
from event_bus import (
    on as _bus_on,  # noqa: E402 — must follow SETTINGS_EVENT/_cache defs
)

_bus_on(SETTINGS_EVENT, drop_cache)
