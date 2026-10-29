"""The agent bootstrap prompt is the AGENT_BOOTSTRAP_PROMPT admin setting — an
admin override reaches the next turn's prompt, Reset returns the shipped text."""

import pytest
import pytest_asyncio
import settings
from agent_config import (
    DEFAULT_BOOTSTRAP_PROMPT,
    build_prompt_and_skill_docs,
    render_bootstrap,
)

import config

_URL = "/api/admin/settings/AGENT_BOOTSTRAP_PROMPT"


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
    override = f"OVERRIDE-MARKER bootstrap. Embeds: {config.BOOTSTRAP_EMBED_TOKEN}."

    resp = await client.put(_URL, json={"value": override}, cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    prompt = (await build_prompt_and_skill_docs(pid))[0]
    assert prompt.startswith(render_bootstrap(override))
    assert DEFAULT_BOOTSTRAP_PROMPT not in prompt

    resp = await client.delete(_URL, cookies={"lore_session": token})
    assert resp.status_code in (200, 204), resp.text
    prompt = (await build_prompt_and_skill_docs(pid))[0]
    assert prompt.startswith(DEFAULT_BOOTSTRAP_PROMPT)
    assert "OVERRIDE-MARKER" not in prompt


@pytest.mark.asyncio
async def test_non_admin_cannot_rewrite_the_bootstrap(client, regular_user):
    _, token = regular_user
    value = f"rewritten {config.BOOTSTRAP_EMBED_TOKEN}"
    resp = await client.put(_URL, json={"value": value}, cookies={"lore_session": token})
    assert resp.status_code == 403
    assert await settings.get("AGENT_BOOTSTRAP_PROMPT") == config.AGENT_BOOTSTRAP_PROMPT
