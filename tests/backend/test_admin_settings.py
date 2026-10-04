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


def _expected_source(env: str, overridden: bool) -> str:
    """The chip's expected state, derived from the runner env (never assumed)."""
    if overridden:
        return "override"
    import os

    return ".env" if env in os.environ else "default"


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


# ─── GET — the derived surface ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_lists_registry_derived_surface(client, admin_user, test_db):
    """GET serves one entry per registry key with its source and effective value."""
    _, token = admin_user
    resp = await client.get("/api/admin/settings", cookies={"lore_session": token})
    assert resp.status_code == 200
    entries = {e["key"]: e for e in resp.json()["settings"]}
    assert set(entries) == {e.key for e in settings_registry.REGISTRY}

    entry = entries["MAX_IMAGE_SIZE_MB"]
    for field in ("env", "tab", "section", "type", "label", "help", "effect", "source", "value"):
        assert field in entry, f"MAX_IMAGE_SIZE_MB missing {field}"
    assert entry["effect"] == "live"
    assert entry["value"] == (await settings.get("MAX_IMAGE_SIZE_MB"))
    assert entry["source"] == _expected_source(entry["env"], overridden=False)

    # A restart key is served read-only with the process's effective env value.
    restart = entries["STT_CONCURRENCY"]
    assert restart["effect"] == "restart"
    assert restart["source"] in (".env", "default")

    # An override flips exactly that key's source/value.
    await test_db.query(
        "CREATE instance_settings CONTENT { key: 'MAX_IMAGE_SIZE_MB', "
        "value: $v, updated_by: 'x', updated_at: time::now() }",
        {"v": json.dumps(7)},
    )
    settings.drop_cache()
    resp = await client.get("/api/admin/settings", cookies={"lore_session": token})
    entry = {e["key"]: e for e in resp.json()["settings"]}["MAX_IMAGE_SIZE_MB"]
    assert entry["source"] == "override"
    assert entry["value"] == 7


# ─── PUT — override write, validation, event ─────────────────────────────────


@pytest.mark.asyncio
async def test_put_creates_override_drops_cache_and_emits(client, admin_user, test_db, caplog):
    """PUT writes the row, this process's cache drops, and the invalidation
    event is emitted for every OTHER process (worker included)."""
    _, token = admin_user
    seen: list[str] = []

    def _record(**kw) -> None:
        seen.append(kw.get("key"))

    from event_bus import off, on

    on(settings.SETTINGS_EVENT, _record)
    try:
        with caplog.at_level("INFO", logger="routes.admin_settings"):
            resp = await client.put(
                "/api/admin/settings/MAX_IMAGE_SIZE_MB", json={"value": 7},
                cookies={"lore_session": token},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["key"] == "MAX_IMAGE_SIZE_MB"
        assert body["source"] == "override"

        rows = await test_db.query(
            # `value` LAST: as the first projection it parses as SELECT VALUE …
            # and the statement is a syntax error (SurrealDB keyword collision).
            "SELECT updated_by, value FROM instance_settings WHERE key = $k",
            {"k": "MAX_IMAGE_SIZE_MB"},
        )
        assert rows and rows[0]["value"] == json.dumps(7)
        assert rows[0]["updated_by"] == "test-admin-001"

        # Same-process read sees the override immediately (cache dropped).
        assert await settings.get("MAX_IMAGE_SIZE_MB") == 7
        for _ in range(10):
            await asyncio.sleep(0)
        assert "MAX_IMAGE_SIZE_MB" in seen
        assert "instance_setting_changed" in caplog.text
    finally:
        off(settings.SETTINGS_EVENT, _record)


@pytest.mark.asyncio
async def test_put_validation(client, admin_user):
    """Unknown key → 404; restart key → 422; wrong type / below min → 422."""
    _, token = admin_user
    resp = await client.put(
        "/api/admin/settings/NO_SUCH_KEY", json={"value": 1},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404

    resp = await client.put(
        "/api/admin/settings/THIN_CRON_HOUR", json={"value": 5},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422

    resp = await client.put(
        "/api/admin/settings/MAX_IMAGE_SIZE_MB", json={"value": "big"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422

    resp = await client.put(
        "/api/admin/settings/MAX_IMAGE_SIZE_MB", json={"value": 0},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_bool_rejected_for_int_key(client, admin_user):
    """Python bools are ints — an int key must still refuse True."""
    _, token = admin_user
    resp = await client.put(
        "/api/admin/settings/MAX_IMAGE_SIZE_MB", json={"value": True},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422


# ─── text settings with a validator (the ComfyUI workflow) ─────────────────────


@pytest.mark.asyncio
async def test_multiline_workflow_put_round_trips_byte_for_byte(client, admin_user):
    """A pasted multiline graph is stored and served back exactly — the admin
    field's Save-disabled check compares strings."""
    from helpers import COMFY_TEST_WORKFLOW

    _, token = admin_user
    wf = dict(COMFY_TEST_WORKFLOW)
    wf["3"] = {**wf["3"], "_meta": {"title": "KSampler"}}  # a fixed-seed workflow
    raw = json.dumps(wf, indent=2) + "\n"
    resp = await client.put(
        "/api/admin/settings/COMFYUI_WORKFLOW", json={"value": raw},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == raw
    assert await settings.get("COMFYUI_WORKFLOW") == raw


@pytest.mark.asyncio
async def test_workflow_with_unknown_marker_is_refused_and_not_written(
    client, admin_user, test_db,
):
    from helpers import COMFY_TEST_WORKFLOW

    _, token = admin_user
    wf = dict(COMFY_TEST_WORKFLOW)
    wf["3"] = {**wf["3"], "_meta": {"title": "KSampler [lore:foo]"}}
    resp = await client.put(
        "/api/admin/settings/COMFYUI_WORKFLOW", json={"value": json.dumps(wf)},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422, resp.text
    assert "[lore:foo]" in resp.text and "node 3" in resp.text
    rows = await test_db.query(
        "SELECT * FROM instance_settings WHERE key = $k", {"k": "COMFYUI_WORKFLOW"},
    )
    assert rows == []


@pytest.mark.asyncio
async def test_size_with_spaces_is_refused(client, admin_user):
    _, token = admin_user
    resp = await client.put(
        "/api/admin/settings/COMFYUI_SIZE_PORTRAIT", json={"value": "832 x 1216"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422, resp.text


# ─── DELETE — reset to env/default ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_row_and_reverts_value(client, admin_user, test_db):
    """DELETE = reset: the row is REMOVED (not written null), so 'overridden'
    stays `row exists`; the value falls back to env/default."""
    _, token = admin_user
    resp = await client.put(
        "/api/admin/settings/MAX_IMAGE_SIZE_MB", json={"value": 7},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200

    import config

    resp = await client.delete(
        "/api/admin/settings/MAX_IMAGE_SIZE_MB", cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["reset"] is True
    rows = await test_db.query(
        "SELECT * FROM instance_settings WHERE key = $k", {"k": "MAX_IMAGE_SIZE_MB"},
    )
    assert rows == []
    assert await settings.get("MAX_IMAGE_SIZE_MB") == config.MAX_IMAGE_SIZE_MB

    resp = await client.delete(
        "/api/admin/settings/NO_SUCH_KEY", cookies={"lore_session": token},
    )
    assert resp.status_code == 404


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


@pytest.mark.asyncio
async def test_get_row_shows_fallback_effective_value(
    client, admin_user, test_db, monkeypatch,
):
    """The admin GET row of a dependent key carries the value the READERS use:
    PUT AI_API_URL → the STT_API_URL row (own row absent, own env unset) shows
    `source: default` with the base override as its effective value — not the
    stale import-time fold `getattr(config, key)` would keep serving."""
    import config

    monkeypatch.delenv("STT_API_URL", raising=False)
    monkeypatch.delenv("AI_API_URL", raising=False)
    monkeypatch.setattr(config, "STT_API_URL", "http://stale-fold")
    _, token = admin_user

    resp = await client.put(
        "/api/admin/settings/AI_API_URL", json={"value": "http://override.test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200

    resp = await client.get("/api/admin/settings", cookies={"lore_session": token})
    assert resp.status_code == 200
    entries = {e["key"]: e for e in resp.json()["settings"]}
    stt_row = entries["STT_API_URL"]
    assert stt_row["source"] == "default", "the dependent's own row is un-set"
    assert stt_row["value"] == "http://override.test"


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


# ─── cross-process invalidation ───────────────────────────────────────────────


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
async def test_internal_knobs_are_neither_listed_nor_editable(client, admin_user):
    """The seven purely internal knobs are constants, not admin rows:
    GET serves none of them and a PUT on each is a 404 —
    the same contract the wiring keys carry above. Why: a knob nobody can
    meaningfully choose (a lock TTL sized against its own heartbeat, a cache
    TTL, a read-slice window) is an admin row that only invites drift from the
    value the surrounding code was sized for."""
    _, token = admin_user
    resp = await client.get("/api/admin/settings", cookies={"lore_session": token})
    assert resp.status_code == 200
    served = {row["key"] for row in resp.json()["settings"]}
    for key in (
        "MEM_RUN_KEY_TTL_S", "BACKPLANE_PUBLISH_TIMEOUT_S",
        "FLUSH_SNAPSHOT_MIN_INTERVAL_SEC", "CHAT_MODELS_CACHE_TTL_S",
        "AGENT_READ_SLICE_CHARS", "TURN_LOCK_TTL_S", "TURN_LOCK_HEARTBEAT_S",
    ):
        assert key not in served, f"{key} must not be an admin setting"
        put = await client.put(
            f"/api/admin/settings/{key}", json={"value": 1},
            cookies={"lore_session": token},
        )
        assert put.status_code == 404, f"PUT {key} must be unknown, got {put.status_code}"


@pytest.mark.asyncio
async def test_backplane_event_drops_cache(client, admin_user):
    """A backplane-delivered instance_settings_changed (a web PUT seen from
    another process) drops this process's override cache."""
    settings._cache = {"PROBE_KEY": json.dumps(1)}
    payload = json.dumps({
        "src": "foreign-process",
        "type": settings.SETTINGS_EVENT,
        "kwargs": {"key": "PROBE_KEY"},
    }).encode()
    from event_bus import _on_backplane_event

    await _on_backplane_event(payload)
    for _ in range(10):
        await asyncio.sleep(0)
    assert settings._cache is None, "backplane delivery must drop the override cache"


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
