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


import pytest
import pytest_asyncio
import settings
from test_harness_turn import _body, _harness_chat  # harness_env: conftest-wide

#: Lore setting value → (dsh provider id, credential key). Stated here on
#: purpose: it is the per-turn contract the harness plugin reads, not a mirror
#: of the payload builder's table.
EXPECTED = {
    "deepseek": ("deepseek-official", "DEEPSEEK_API_KEY"),
    "brave": ("lore-brave", "BRAVE_API_KEY"),
    "tavily": ("lore-tavily", "TAVILY_API_KEY"),
    "searxng": ("lore-searxng", "SEARXNG_URL"),
    "perplexity": ("lore-perplexity-fast", "PERPLEXITY_API_KEY"),
    # The router searches with one of its own models; its upstream key never
    # enters Lore (plugin web-search/router.ts).
    "router": ("lore-router", "WEB_SEARCH_MODEL"),
}
#: PERPLEXITY_SEARCH_TYPE value → the pinned id; the harness registers one
#: Perplexity provider per search type (plugin web-search/perplexity.ts).
EXPECTED_PERPLEXITY_PINS = {
    "fast": "lore-perplexity-fast",
    "web": "lore-perplexity-web",
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
    for key in ("WEB_SEARCH_PROVIDER", "PERPLEXITY_SEARCH_TYPE", "CHAT_TITLE_MODEL", "CHAT_MODEL", *CREDENTIAL_KEYS):
        monkeypatch.delenv(key, raising=False)
    _pin_config(
        monkeypatch,
        WEB_SEARCH_PROVIDER="deepseek",
        PERPLEXITY_SEARCH_TYPE="fast",
        CHAT_TITLE_MODEL="env-titler",
        CHAT_MODEL="env-titler",
        **{key: f"env-{key.lower()}" for key in CREDENTIAL_KEYS},
    )
    # WHY env too: WEB_SEARCH_MODEL has a fallback, and settings._resolve reads
    # a fallback key's own config value only when its env var is present.
    monkeypatch.setenv("WEB_SEARCH_MODEL", "env-web_search_model")
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


# ─── the payload: per-provider pin + ONLY that provider's credential ─────────


@pytest.mark.asyncio
async def test_perplexity_web_mode_set_in_admin_pins_the_web_provider(
    client, collab_project, harness_env,
):
    """The operator picks perplexity + the web mode in the admin panel: the
    next turn pins the web-mode provider with the Perplexity key."""
    pid, doc_id, admin_token, _user_token, _a, _u = collab_project
    for key, value in (
        ("WEB_SEARCH_PROVIDER", "perplexity"),
        ("PERPLEXITY_SEARCH_TYPE", "web"),
        ("PERPLEXITY_API_KEY", "db-pplx-key"),
    ):
        resp = await client.put(
            f"/api/admin/settings/{key}", json={"value": value},
            cookies={"lore_session": admin_token},
        )
        assert resp.status_code == 200, resp.text
    payload = await _turn_payload_of(client, collab_project, harness_env)
    assert payload["web_search_provider"] == EXPECTED_PERPLEXITY_PINS["web"]
    assert payload["web_search_credential"] == "db-pplx-key"
