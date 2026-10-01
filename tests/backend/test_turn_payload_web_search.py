"""The turn payload's admin-config trio: title model + web-search provider/credential.

The session title model and the web-search provider + its ONE credential
reach the harness PER TURN — the same path AI_API_URL/KEY ride — so an admin
change applies from the next message with no `docker compose restart harness`.
Web search has NO off state: the provider is always pinned (an unpinned one
would make dsh silently pick any usable provider — DeepSeek with a resolver is
always usable), and to stop searching an admin clears the key/URL: the call
then fails loudly naming the provider and the admin path. The boot-time driver
endpoint is gone with its job: GET /api/driver/web-search 404s.
"""

import json
import os
import subprocess
import sys

import pytest
import pytest_asyncio
import settings
import settings_registry
from test_harness_turn import _body, _harness_chat  # harness_env: conftest-wide

#: Lore setting value → (dsh provider id, credential key). Stated here on
#: purpose: it is the per-turn contract the harness plugin reads, not a mirror
#: of the payload builder's table.
EXPECTED = {
    "deepseek": ("deepseek-official", "DEEPSEEK_API_KEY"),
    "brave": ("lore-brave", "BRAVE_API_KEY"),
    "tavily": ("lore-tavily", "TAVILY_API_KEY"),
    "searxng": ("lore-searxng", "SEARXNG_URL"),
}
CREDENTIAL_KEYS = [cred for _, cred in EXPECTED.values()]


def _pin_config(monkeypatch, **values: str) -> None:
    """The file's ONE config setattr seam (the import-shape ratchet counts
    sites): pin config attrs for a test."""
    import config

    for key, value in values.items():
        monkeypatch.setattr(config, key, value)


@pytest_asyncio.fixture(autouse=True)
async def _pinned_payload_flags(test_db, monkeypatch):
    """Every flag that shapes the trio, pinned in ONE place.

    # INVARIANT(exact-surface): the payload's three fields are asserted equal
    # to values this fixture owns end-to-end — config attrs pinned, the keys'
    # env vars scrubbed (settings._resolve prefers an env-present base key
    # over the fallback walk, so an exported CHAT_TITLE_MODEL would answer the
    # unset-title-model case with the runner's own value), DB override rows
    # deleted, the override cache dropped before AND after.
    """
    settings.drop_cache()
    await test_db.query("DELETE instance_settings")
    for key in ("WEB_SEARCH_PROVIDER", "CHAT_TITLE_MODEL", "CHAT_MODEL", *CREDENTIAL_KEYS):
        monkeypatch.delenv(key, raising=False)
    _pin_config(
        monkeypatch,
        WEB_SEARCH_PROVIDER="deepseek",
        CHAT_TITLE_MODEL="env-titler",
        CHAT_MODEL="env-titler",
        **{key: f"env-{key.lower()}" for key in CREDENTIAL_KEYS},
    )
    yield
    settings.drop_cache()
    await test_db.query("DELETE instance_settings")


async def _turn_payload_of(client, collab_project, harness_env) -> dict:
    pid, doc_id, _admin_token, user_token, _a, _u = collab_project
    sid = await _harness_chat(client, pid, doc_id, user_token)
    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions", json=_body(user_token),
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 200, resp.text
    assert len(harness_env.followups.payloads) == 1
    return harness_env.followups.payloads[0]


# ─── the registry: choices + default ─────────────────────────────────────────


def test_every_provider_choice_has_a_payload_mapping():
    """Derived from the registry, not a literal: a new choice without a
    mapping fails here (the payload builder would KeyError every turn)."""
    spec = settings_registry.find("WEB_SEARCH_PROVIDER")
    assert spec is not None and spec.choices is not None
    assert set(spec.choices) == set(EXPECTED)


def test_off_is_gone_and_the_default_is_deepseek():
    """No off state: `off` is not a choice, and a fresh install (no env) boots
    deepseek — the no-key failure names the provider and the admin path."""
    spec = settings_registry.find("WEB_SEARCH_PROVIDER")
    assert "off" not in (spec.choices or ())
    # The default is import-time folded: prove it in a fresh interpreter with
    # the env var scrubbed — the in-process config was shaped by this suite.
    probe = subprocess.run(
        [sys.executable, "-c", "import config; print(config.WEB_SEARCH_PROVIDER)"],
        env={k: v for k, v in os.environ.items() if k != "WEB_SEARCH_PROVIDER"},
        capture_output=True, text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "deepseek"


# ─── the payload: per-provider pin + ONLY that provider's credential ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(EXPECTED))
async def test_each_provider_turn_carries_its_pin_and_only_its_credential(
    client, collab_project, harness_env, monkeypatch, provider,
):
    _pin_config(monkeypatch, WEB_SEARCH_PROVIDER=provider)
    payload = await _turn_payload_of(client, collab_project, harness_env)
    pin, cred = EXPECTED[provider]
    assert payload["web_search_provider"] == pin
    assert payload["web_search_credential"] == f"env-{cred.lower()}"
    # No UNSELECTED provider's credential value reaches the payload at all —
    # over the serialized form, so an accidental extra field fails too.
    unselected = [
        f"env-{c.lower()}" for p, (_, c) in EXPECTED.items() if p != provider
    ]
    assert all(value not in json.dumps(payload) for value in unselected)
    assert payload["title_model"] == "env-titler"


@pytest.mark.asyncio
async def test_admin_override_set_after_start_is_in_the_next_payload(
    client, collab_project, harness_env,
):
    """The operator story: everything is set in the admin panel on a running
    install — provider, key, title model — and the NEXT message carries it."""
    pid, doc_id, admin_token, _user_token, _a, _u = collab_project
    for key, value in (
        ("WEB_SEARCH_PROVIDER", "brave"),
        ("BRAVE_API_KEY", "db-brave-key"),
        ("CHAT_TITLE_MODEL", "db-titler"),
    ):
        resp = await client.put(
            f"/api/admin/settings/{key}", json={"value": value},
            cookies={"lore_session": admin_token},
        )
        assert resp.status_code == 200, resp.text
    payload = await _turn_payload_of(client, collab_project, harness_env)
    assert payload["web_search_provider"] == "lore-brave"
    assert payload["web_search_credential"] == "db-brave-key"
    assert payload["title_model"] == "db-titler"


@pytest.mark.asyncio
async def test_unset_title_model_falls_back_to_the_chat_model(
    client, collab_project, harness_env, monkeypatch,
):
    _pin_config(monkeypatch, CHAT_TITLE_MODEL="", CHAT_MODEL="local/orange/chat")
    payload = await _turn_payload_of(client, collab_project, harness_env)
    assert payload["title_model"] == "local/orange/chat"


@pytest.mark.asyncio
async def test_unset_credential_rides_as_an_empty_string(
    client, collab_project, harness_env, monkeypatch,
):
    """A fresh install has no key: the field is ALWAYS present, empty — the
    harness then fails every search loudly (there is no off to fall back to)."""
    _pin_config(monkeypatch, DEEPSEEK_API_KEY="")
    payload = await _turn_payload_of(client, collab_project, harness_env)
    assert payload["web_search_provider"] == "deepseek-official"
    assert payload["web_search_credential"] == ""


# ─── the boot endpoint is gone ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_driver_boot_endpoint_is_gone(client):
    """The harness boots without a backend round trip now; the old endpoint
    (it carried a plaintext key) must not linger."""
    resp = await client.get(
        "/api/driver/web-search", headers={"X-Driver-Secret": "anything"})
    assert resp.status_code == 404, resp.text
