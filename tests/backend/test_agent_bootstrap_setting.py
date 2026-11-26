"""The two admin texts of the system prompt — AGENT_BOOTSTRAP_PROMPT (the
floor) and AGENT_BASE_PERSONA_PROMPT — reach the next turn's prompt in that
order; Reset returns the shipped text of each."""

import pytest
import pytest_asyncio
import settings
from agent_config import (
    DEFAULT_BASE_PERSONA_PROMPT,
    DEFAULT_BOOTSTRAP_PROMPT,
    build_prompt_and_skill_docs,
)

import config

_FLOOR = "/api/admin/settings/AGENT_BOOTSTRAP_PROMPT"
_BASE = "/api/admin/settings/AGENT_BASE_PERSONA_PROMPT"


@pytest_asyncio.fixture(autouse=True)
async def _clean_settings_state(test_db):
    """No override rows and no cached overrides around every test."""
    settings.drop_cache()
    await test_db.query("DELETE instance_settings")
    yield
    settings.drop_cache()
    await test_db.query("DELETE instance_settings")


@pytest.mark.asyncio
async def test_admin_override_reaches_the_turn_and_reset_restores_default(
    client, admin_user, project_with_doc,
):
    pid, _idx, _admin = project_with_doc
    _, token = admin_user
    overrides = {_FLOOR: "OVERRIDE-MARKER floor.", _BASE: "OVERRIDE-MARKER base persona."}

    for url, value in overrides.items():
        resp = await client.put(url, json={"value": value}, cookies={"lore_session": token})
        assert resp.status_code == 200, resp.text
    prompt = (await build_prompt_and_skill_docs(pid))[0]
    assert prompt.startswith("OVERRIDE-MARKER floor.\n\nOVERRIDE-MARKER base persona."), (
        "the floor opens the prompt and the base persona follows it")
    assert DEFAULT_BOOTSTRAP_PROMPT not in prompt
    assert DEFAULT_BASE_PERSONA_PROMPT not in prompt

    for url in overrides:
        resp = await client.delete(url, cookies={"lore_session": token})
        assert resp.status_code in (200, 204), resp.text
    prompt = (await build_prompt_and_skill_docs(pid))[0]
    assert prompt.startswith(f"{DEFAULT_BOOTSTRAP_PROMPT}\n\n{DEFAULT_BASE_PERSONA_PROMPT}")
    assert "OVERRIDE-MARKER" not in prompt


@pytest.mark.asyncio
async def test_non_admin_cannot_rewrite_the_bootstrap(client, regular_user):
    _, token = regular_user
    for url, key in ((_FLOOR, "AGENT_BOOTSTRAP_PROMPT"), (_BASE, "AGENT_BASE_PERSONA_PROMPT")):
        resp = await client.put(url, json={"value": "rewritten"}, cookies={"lore_session": token})
        assert resp.status_code == 403, url
        assert await settings.get(key) == getattr(config, key)
