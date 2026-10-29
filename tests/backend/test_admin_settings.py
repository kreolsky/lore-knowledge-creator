"""API tests for admin instance-settings endpoints — list, override, reset, RBAC.

The endpoints are admin-ONLY (require_admin): a moderator passes the user-manager
gate but must not reach instance-wide settings — a settings change is a
server-wide act with no per-project scope.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import settings
import settings_registry
from helpers import make_token
from password import hash_secret
from settings_registry import SettingSpec

from db import create_record


@pytest_asyncio.fixture(autouse=True)
async def _clean_settings_state(test_db):
    """No override rows and no cached overrides around every test."""
    settings.drop_cache()
    await test_db.query("DELETE instance_settings")
    yield
    settings.drop_cache()
    await test_db.query("DELETE instance_settings")


@pytest_asyncio.fixture
async def moderator_user(test_db):
    """A role='moderator' principal — passes require_user_manager_role, not require_admin."""
    uid = "test-mod-settings-001"
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "testmod-settings",
        "email": "mod-settings@test.com",
        "password_hash": hash_secret("pass123"),
        "role": "moderator",
        "user_facts": "",
    })
    token = make_token(uid, "testmod-settings", "moderator", "mod-settings@test.com")
    yield uid, token
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})


# ─── RBAC ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_regular_user_forbidden_everywhere(client, regular_user):
    """A role='user' principal gets 403 on GET, PUT and DELETE."""
    _, token = regular_user
    resp = await client.get("/api/admin/settings", cookies={"lore_session": token})
    assert resp.status_code == 403
    resp = await client.put(
        "/api/admin/settings/MAX_IMAGE_SIZE_MB", json={"value": 7},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 403
    resp = await client.delete(
        "/api/admin/settings/MAX_IMAGE_SIZE_MB", cookies={"lore_session": token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_moderator_forbidden_everywhere(client, moderator_user):
    """A moderator reaches /api/admin/users but NOT settings — require_admin is
    the only thing keeping them off this surface, so it is tested directly."""
    _, token = moderator_user
    resp = await client.get("/api/admin/settings", cookies={"lore_session": token})
    assert resp.status_code == 403
    resp = await client.put(
        "/api/admin/settings/MAX_IMAGE_SIZE_MB", json={"value": 7},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 403
    resp = await client.delete(
        "/api/admin/settings/MAX_IMAGE_SIZE_MB", cookies={"lore_session": token},
    )
    assert resp.status_code == 403


# ─── PUT — override write, validation, event ─────────────────────────────────


@pytest.mark.asyncio
async def test_put_bool_rejected_for_int_key(client, admin_user):
    """Python bools are ints — an int key must still refuse True."""
    _, token = admin_user
    resp = await client.put(
        "/api/admin/settings/MAX_IMAGE_SIZE_MB", json={"value": True},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422


# ─── fallback chains — derived defaults re-derived at the reader ──────────────


@pytest.mark.asyncio
async def test_get_walks_stt_api_fallback_base_override_reaches_dependent(
    client, admin_user, test_db, monkeypatch,
):
    """The two-link STT_API_URL ← AI_API_URL chain held on the registry and
    walked by settings.get: an override row on the BASE key surfaces through
    the dependent's read, while the dependent's folded config value
    (import-time fold of the base's ENV value) would never move."""
    import config

    monkeypatch.delenv("STT_API_URL", raising=False)
    monkeypatch.delenv("AI_API_URL", raising=False)
    monkeypatch.setattr(config, "STT_API_URL", "http://stale-fold")
    _, token = admin_user

    # No rows, dependent and base env unset: the walk resolves the last link.
    assert await settings.get("STT_API_URL") == config.AI_API_URL

    resp = await client.put(
        "/api/admin/settings/AI_API_URL", json={"value": "http://override.test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert await settings.get("STT_API_URL") == "http://override.test", (
        "a base-key override must reach the dependent's read"
    )

    resp = await client.put(
        "/api/admin/settings/STT_API_URL", json={"value": "http://own.test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert await settings.get("STT_API_URL") == "http://own.test", (
        "the dependent's own override outranks the base's"
    )


# ─── secrets — masked GET, mask-write is a no-op ──────────────────────────────


_FAKE_SECRET = SettingSpec(
    key="FAKE_SECRET_KEY", env="FAKE_SECRET_KEY", tab="storage", section="Storage",
    type="secret", label="Fake secret", help="test-only registry entry",
    effect="live",
)


@pytest.mark.asyncio
async def test_secret_masked_in_get_and_mask_put_is_noop(client, admin_user, monkeypatch):
    """A secret never leaves the server in clear; writing the mask back is a no-op."""
    import config

    # Both read paths resolve off the ONE index: the GET listing iterates
    # BY_KEY.values() and PUT/settings.get go through find → BY_KEY — patching
    # the index alone reaches every surface (the import-shape gate counts a
    # seam per monkeypatch.setattr call site, so one target beats two).
    monkeypatch.setattr(
        settings_registry, "BY_KEY",
        {**settings_registry.BY_KEY, _FAKE_SECRET.key: _FAKE_SECRET},
    )
    monkeypatch.setattr(config, "FAKE_SECRET_KEY", "s3cr3tvalu3", raising=False)
    _, token = admin_user

    resp = await client.get("/api/admin/settings", cookies={"lore_session": token})
    entry = {e["key"]: e for e in resp.json()["settings"]}["FAKE_SECRET_KEY"]
    assert entry["value"] == "••••alu3"
    assert "s3cr3tvalu3" not in resp.text

    # Writing the mask back must not persist the mask string as the value.
    resp = await client.put(
        "/api/admin/settings/FAKE_SECRET_KEY", json={"value": "••••alu3"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert await settings.get("FAKE_SECRET_KEY") == "s3cr3tvalu3"

    # A real write stores plaintext (same trust boundary as .env)…
    resp = await client.put(
        "/api/admin/settings/FAKE_SECRET_KEY", json={"value": "newsecret"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert await settings.get("FAKE_SECRET_KEY") == "newsecret"
    # …and GET still only ever answers masked.
    resp = await client.get("/api/admin/settings", cookies={"lore_session": token})
    entry = {e["key"]: e for e in resp.json()["settings"]}["FAKE_SECRET_KEY"]
    assert entry["value"] == "••••cret"
    assert "newsecret" not in resp.text


# ─── wiring is not configuration (plan component-wiring-not-settings) ────────


@pytest.mark.asyncio
async def test_wiring_keys_are_neither_listed_nor_editable(client, admin_user):
    """The driver line's address/secret/secret-file, the converter URL, the
    session signing key and the storage/Redis addresses are constants — the
    admin surface serves none of them and refuses a PUT with 404. Why: every
    way to set one was a way to make the backend and the harness disagree (a
    secret typed in a v0.20.3 admin panel shadowed the generated file after
    upgrade and killed the agent behind a 401), and the infra addresses are
    compose wiring nobody ever changes (plan component-wiring-not-settings)."""
    _, token = admin_user
    resp = await client.get("/api/admin/settings", cookies={"lore_session": token})
    assert resp.status_code == 200
    served = {row["key"] for row in resp.json()["settings"]}
    for key in (
        "HARNESS_DRIVER_URL", "HARNESS_DRIVER_SECRET",
        "HARNESS_DRIVER_SECRET_FILE", "CONVERTER_URL",
        "SECRET_KEY", "STORAGE_PATH", "REDIS_URL",
    ):
        assert key not in served, f"{key} must not be an admin setting"
        put = await client.put(
            f"/api/admin/settings/{key}", json={"value": "x"},
            cookies={"lore_session": token},
        )
        assert put.status_code == 404, f"PUT {key} must be unknown, got {put.status_code}"


@pytest.mark.asyncio
async def test_worker_startup_subscribes_invalidation(client, admin_user):
    """The worker process registers drop_cache and joins the backplane at
    startup, so a live override takes effect on its next job."""
    import event_bus
    import jobs.worker as worker_mod
    from event_bus import off, on

    settings.drop_cache()
    off(settings.SETTINGS_EVENT, settings.drop_cache)
    try:
        await worker_mod._on_worker_startup({"redis": AsyncMock()})
        handlers = event_bus._subscribers.get(settings.SETTINGS_EVENT, [])
        assert settings.drop_cache in handlers, "worker startup must register drop_cache"

        settings._cache = {"PROBE_KEY": json.dumps(1)}
        payload = json.dumps({
            "src": "foreign-process",
            "type": settings.SETTINGS_EVENT,
            "kwargs": {"key": "PROBE_KEY"},
        }).encode()
        await event_bus._on_backplane_event(payload)
        for _ in range(10):
            await asyncio.sleep(0)
        assert settings._cache is None
    finally:
        off(settings.SETTINGS_EVENT, settings.drop_cache)
        on(settings.SETTINGS_EVENT, settings.drop_cache)
