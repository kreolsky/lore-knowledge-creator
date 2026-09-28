"""Stage 3/9 tests — the turn contract (drop history replay, decision 5).

# SYSTEM: driver turn contract — the payload carries NO messages[], only the
# last user turn (`prompt`, multimodal) + the layered system_prompt + the session
# id. The named system-prompt overlay (layer a) is present in every emitted
# system_prompt. Stage 9 made this the only contract (the legacy agentLoop path
# is deleted).
"""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# [sent 2026-09-01T23:14:05+03:00] — the per-turn send stamp (the full stamp
# suite lives in test_prepare_agent_turn).
SENT_STAMP_RE = re.compile(
    r"\[sent \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\]$"
)


def test_payload_has_no_messages_and_carries_prompt():
    """Contract test (decision 5): the payload omits messages[] and sends only
    `prompt` (last user turn, multimodal) + the session id + system_prompt."""
    from driver.client import _build_turn_payload

    payload = _build_turn_payload(
        model="m", system_prompt="SP",
        tools=[], agent_key="k", apply_mode="auto",
        session_id="s1", user_id="u1", project_id="p1", document_id="d1",
        prompt=[{"type": "text", "text": "hi"}],
    )
    assert "messages" not in payload, "payload must NOT carry messages[]"
    assert payload["prompt"] == [{"type": "text", "text": "hi"}]
    assert payload["session_id"] == "s1"
    assert payload["system_prompt"] == "SP"
    assert payload["harness"] is True


def _fake_body(*, messages=None, auto_apply=False, model=None):
    # region mirrors CompletionBody.region (default None — non-pinned session);
    # context_ids mirrors CompletionRequest.context_ids (the agent Parent-line gate).
    return SimpleNamespace(
        messages=messages or [], auto_apply=auto_apply, region=None, context_ids=None,
        open_doc_id=None, model=model,
    )


def _fake_ctx(system_prefix=None, image_parts=None):
    return SimpleNamespace(
        system_prefix=system_prefix or [], sources=[], warnings=[],
        image_parts=image_parts or [],
    )


@pytest.mark.asyncio
async def test_named_system_prompt_overlay_is_present_in_every_mode():
    """Layer (a) — the named system prompt (from build_context, body.system_prompt_id)
    is folded into system_prompt regardless of apply mode. The build_context output
    (ctx.system_prefix) carries the overlay; prepare_agent_turn prepends it."""
    from routes.chat.completions import prepare_agent_turn

    overlay = {"role": "system", "content": "You are a grim historian."}
    body = _fake_body(
        messages=[SimpleNamespace(role="user", content="hello", images=None)],
        auto_apply=False,
    )
    ctx = _fake_ctx(system_prefix=[overlay])

    with patch(
        "routes.chat.completions_turn.build_prompt_and_skill_docs",
        new=AsyncMock(return_value=("PERSONA", [])),
    ), patch(
        "routes.chat.completions_turn._build_current_document_section",
        new=AsyncMock(return_value=None),
    ), patch(
        "routes.chat.completions_turn.fetch_one", new=AsyncMock(return_value=None),
    ):
        plan = await prepare_agent_turn(
            session={"document_id": "d1"}, body=body, ctx=ctx, project_id="p1",
            user={"user_id": "u1"}, toolset=[], capability_is_mutating=True,
        )

    # The overlay (layer a) is present verbatim in the assembled system_prompt.
    assert "You are a grim historian." in plan.system_prompt
    # Layer (c) persona is also present.
    assert "PERSONA" in plan.system_prompt
    # Stage 3: prompt is the last user turn content. The send stamp rides the
    # tail (UTC here — fetch_one stubbed to no users row); the stable part is
    # asserted, stamp bytes belong to the stamp suite.
    assert plan.prompt.startswith("hello")
    assert SENT_STAMP_RE.search(plan.prompt)


@pytest.mark.asyncio
async def test_prompt_is_multimodal_when_last_turn_has_images():
    """Decision 5: prompt keeps the multimodal shape (text + image_url data-URI
    parts) exactly as _build_last_user_prompt emits for the last user turn."""
    from routes.chat.completions import prepare_agent_turn

    body = _fake_body(
        messages=[SimpleNamespace(
            role="user", content="describe this",
            images=["data:image/png;base64,AAAA"],
        )],
        model="local/orange/chat",
    )
    ctx = _fake_ctx()

    with patch(
        "routes.chat.completions_turn.build_prompt_and_skill_docs",
        new=AsyncMock(return_value=("P", [])),
    ), patch(
        "routes.chat.completions_turn._build_current_document_section",
        new=AsyncMock(return_value=None),
    ), patch(
        "routes.chat.completions_turn.fetch_one", new=AsyncMock(return_value=None),
    ):
        plan = await prepare_agent_turn(
            session={"document_id": "d1"}, body=body, ctx=ctx, project_id="p1",
            user={"user_id": "u1"}, toolset=[], capability_is_mutating=True,
        )

    # Stamp rides parts[0].text; the image part is asserted unchanged and
    # still last (stamp bytes pinned in test_prepare_agent_turn).
    assert isinstance(plan.prompt, list)
    assert plan.prompt[0]["type"] == "text"
    assert plan.prompt[0]["text"].startswith("describe this")
    assert SENT_STAMP_RE.search(plan.prompt[0]["text"])
    assert plan.prompt[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }


@pytest.mark.asyncio
async def test_context_image_parts_merged_into_prompt():
    """Context image references (ctx.image_parts) must reach the model via the
    `prompt` channel — the only Pi path that renders images. Order: user text →
    context image parts → user-attached images. Regression: Stage 9 collapsed
    context to a string, silently dropping image bytes."""
    from routes.chat.completions import prepare_agent_turn

    context_img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,Q09OVEVYVA=="}}
    body = _fake_body(
        messages=[SimpleNamespace(
            role="user", content="what is this",
            images=["data:image/png;base64,VVNFUg=="],
        )],
        model="local/orange/chat",
    )
    ctx = _fake_ctx(image_parts=[context_img])

    with patch(
        "routes.chat.completions_turn.build_prompt_and_skill_docs",
        new=AsyncMock(return_value=("P", [])),
    ), patch(
        "routes.chat.completions_turn._build_current_document_section",
        new=AsyncMock(return_value=None),
    ), patch(
        "routes.chat.completions_turn.fetch_one", new=AsyncMock(return_value=None),
    ):
        plan = await prepare_agent_turn(
            session={"document_id": "d1"}, body=body, ctx=ctx, project_id="p1",
            user={"user_id": "u1"}, toolset=[], capability_is_mutating=True,
        )

    assert isinstance(plan.prompt, list), "prompt must be a list when context images present"
    img_urls = [p["image_url"]["url"] for p in plan.prompt if p.get("type") == "image_url"]
    # Both the context image and the user-attached image survive.
    assert "data:image/png;base64,Q09OVEVYVA==" in img_urls
    assert "data:image/png;base64,VVNFUg==" in img_urls
    # Order: user text first, context image before user-attached image.
    assert plan.prompt[0]["type"] == "text"
    assert plan.prompt[1] == context_img


@pytest.mark.asyncio
async def test_context_image_parts_merged_when_prompt_is_str():
    """A str prompt + context images → list with text part + image parts."""
    from routes.chat.completions import prepare_agent_turn

    context_img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,Q09OVEVYVA=="}}
    body = _fake_body(
        messages=[SimpleNamespace(role="user", content="describe", images=None)],
    )
    ctx = _fake_ctx(image_parts=[context_img])

    with patch(
        "routes.chat.completions_turn.build_prompt_and_skill_docs",
        new=AsyncMock(return_value=("P", [])),
    ), patch(
        "routes.chat.completions_turn._build_current_document_section",
        new=AsyncMock(return_value=None),
    ), patch(
        "routes.chat.completions_turn.fetch_one", new=AsyncMock(return_value=None),
    ):
        plan = await prepare_agent_turn(
            session={"document_id": "d1"}, body=body, ctx=ctx, project_id="p1",
            user={"user_id": "u1"}, toolset=[], capability_is_mutating=True,
        )

    assert isinstance(plan.prompt, list)
    assert plan.prompt[0]["type"] == "text"
    assert plan.prompt[0]["text"].startswith("describe")
    assert plan.prompt[1] == context_img


def test_prepare_agent_turn_prompt_docstring_records_contract():
    """Self-documenting: the prepare_agent_turn docstring / AgentTurnPlan carries
    the Stage 3 contract (no messages[] on the harness path)."""
    from routes.chat.completions import AgentTurnPlan

    assert "prompt" in AgentTurnPlan.__dataclass_fields__
