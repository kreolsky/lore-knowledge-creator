"""Web-search provider settings + the driver endpoint the harness boots from.

GET /api/driver/web-search hands the harness the env it must boot with: the dsh
provider pin plus ONLY the selected provider's credential. It is attested by the
driver secret, never by a user session — the response carries a plaintext key.
"""

import pytest
import pytest_asyncio
import settings
import settings_registry
from settings_registry import ConfigError, SettingSpec, SettingValueError

URL = "/api/driver/web-search"
SECRET = "drv-web-s3cret"

#: Lore setting value → (dsh provider id, credential key). Stated here on
#: purpose: it is the boot contract the harness plugin reads, not a mirror of
#: an internal table.
EXPECTED = {
    "deepseek": ("deepseek-official", "DEEPSEEK_API_KEY"),
    "brave": ("lore-brave", "BRAVE_API_KEY"),
    "tavily": ("lore-tavily", "TAVILY_API_KEY"),
    "searxng": ("lore-searxng", "SEARXNG_URL"),
}
CREDENTIAL_KEYS = [cred for _, cred in EXPECTED.values()]


@pytest_asyncio.fixture(autouse=True)
async def _clean_settings_state(test_db, monkeypatch):
    """No override rows, no cached overrides, a known driver secret, and every
    web-search credential pinned to a distinct env-layer value."""
    import config

    settings.drop_cache()
    await test_db.query("DELETE instance_settings")
    env_layer = {
        "HARNESS_DRIVER_SECRET": SECRET,
        "WEB_SEARCH_PROVIDER": "off",
        **{key: f"env-{key.lower()}" for key in CREDENTIAL_KEYS},
    }
    for key, value in env_layer.items():
        monkeypatch.setattr(config, key, value)
    yield
    settings.drop_cache()
    await test_db.query("DELETE instance_settings")


# ─── Auth ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_secret_is_refused(client):
    resp = await client.get(URL)
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_wrong_secret_is_refused(client):
    for secret in ("wrong", SECRET[:-1], SECRET + "x"):
        resp = await client.get(URL, headers={"X-Driver-Secret": secret})
        assert resp.status_code == 401, (secret, resp.text)


@pytest.mark.asyncio
async def test_a_user_session_is_not_a_driver(client, admin_user):
    """An admin cookie does not open it: the payload is a plaintext key."""
    _, token = admin_user
    resp = await client.get(URL, cookies={"lore_session": token})
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_unconfigured_driver_secret_refuses_everyone(client, monkeypatch):
    import config

    monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "", raising=False)
    resp = await client.get(URL, headers={"X-Driver-Secret": ""})
    assert resp.status_code == 401, resp.text


# ─── Payload ──────────────────────────────────────────────────────────────────


def test_every_provider_choice_has_a_boot_mapping():
    """Derived from the registry: a new choice without a mapping fails here."""
    spec = settings_registry.find("WEB_SEARCH_PROVIDER")
    assert spec is not None and spec.choices is not None
    assert set(spec.choices) == {"off", *EXPECTED}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(EXPECTED))
async def test_each_provider_serves_exactly_its_env_pair(client, monkeypatch, provider):
    import config

    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", provider)
    pin, cred = EXPECTED[provider]
    resp = await client.get(URL, headers={"X-Driver-Secret": SECRET})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"DSH_WEB_SEARCH_PROVIDER": pin, cred: f"env-{cred.lower()}"}


@pytest.mark.asyncio
async def test_off_serves_no_pin(client):
    resp = await client.get(URL, headers={"X-Driver-Secret": SECRET})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {}


@pytest.mark.asyncio
async def test_db_override_beats_env(client, admin_user):
    """Admin PUTs (the DB layer) win over the env layer for pin and key alike."""
    _, token = admin_user
    cookies = {"lore_session": token}
    for key, value in (("WEB_SEARCH_PROVIDER", "brave"), ("BRAVE_API_KEY", "db-brave-key")):
        resp = await client.put(f"/api/admin/settings/{key}", json={"value": value}, cookies=cookies)
        assert resp.status_code == 200, resp.text

    resp = await client.get(URL, headers={"X-Driver-Secret": SECRET})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"DSH_WEB_SEARCH_PROVIDER": "lore-brave", "BRAVE_API_KEY": "db-brave-key"}


# ─── choices ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_off_list_value_is_refused_on_put(client, admin_user):
    _, token = admin_user
    resp = await client.put(
        "/api/admin/settings/WEB_SEARCH_PROVIDER", json={"value": "google"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422, resp.text
    assert "google" in resp.json()["detail"]
    assert (await settings.load_overrides()).get("WEB_SEARCH_PROVIDER") is None


@pytest.mark.asyncio
async def test_listing_carries_the_choices(client, admin_user):
    _, token = admin_user
    resp = await client.get("/api/admin/settings", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    by_key = {row["key"]: row for row in resp.json()["settings"]}
    spec = settings_registry.find("WEB_SEARCH_PROVIDER")
    assert by_key["WEB_SEARCH_PROVIDER"]["choices"] == list(spec.choices)
    assert by_key["BRAVE_API_KEY"]["choices"] is None


def test_coerce_refuses_an_off_list_value():
    spec = SettingSpec(
        key="K", env="K", tab="tools", section="S", type="str", label="L",
        help="", effect="live", choices=("a", "b"),
    )
    assert spec.coerce("b") == "b"
    with pytest.raises(SettingValueError):
        spec.coerce("c")


@pytest.fixture
def probe_section(monkeypatch):
    """Aim `setting()` at a probe section, as config.py's `_section()` would."""
    monkeypatch.setattr(settings_registry, "_CURSOR", ("tools", "Probe"))


def test_off_list_env_value_fails_at_declaration(monkeypatch, probe_section):
    """An off-list env value is a deploy error: ConfigError at import, and the
    bad declaration never enters the registry."""
    monkeypatch.setenv("LORE_TEST_CHOICE_PROBE", "c")
    before = len(settings_registry.REGISTRY)
    with pytest.raises(ConfigError):
        settings_registry.setting(
            "LORE_TEST_CHOICE_PROBE", str, default="a", choices=("a", "b"),
            label="probe", help="",
        )
    assert len(settings_registry.REGISTRY) == before
    assert settings_registry.find("LORE_TEST_CHOICE_PROBE") is None


# ─── visible_if: only the selected provider's credential row is shown ─────────


@pytest.mark.asyncio
async def test_each_credential_row_is_visible_only_for_its_provider(client, admin_user):
    _, token = admin_user
    resp = await client.get("/api/admin/settings", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    by_key = {row["key"]: row for row in resp.json()["settings"]}
    for provider, (_, cred) in EXPECTED.items():
        assert by_key[cred]["visible_if"] == {"key": "WEB_SEARCH_PROVIDER", "value": provider}
    assert by_key["WEB_SEARCH_PROVIDER"]["visible_if"] is None


def test_visible_if_naming_no_declared_choice_fails_at_declaration(probe_section):
    before = len(settings_registry.REGISTRY)
    for link in (("WEB_SEARCH_PROVIDER", "google"), ("NO_SUCH_KEY", "x"), ("BRAVE_API_KEY", "x")):
        with pytest.raises(ConfigError):
            settings_registry.setting(
                "LORE_TEST_VISIBLE_IF_PROBE", str, default="", visible_if=link,
                label="probe", help="",
            )
    assert len(settings_registry.REGISTRY) == before
