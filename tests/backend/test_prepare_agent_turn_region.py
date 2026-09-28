"""B4: pinned-fragment prompt hint injection in prepare_agent_turn.

# SYSTEM: chat-region-prompt-tests — # Pinned fragment block in the Pi system prompt.

When a completion request carries a region, the agent system prompt gains a
"# Pinned fragment" block with the region text + a "you may edit ONLY inside this
fragment" instruction. Absent a region, the block is omitted (regression).
"""

from types import SimpleNamespace

import pytest
from agent.apply_policy import CONFIRM, ApplyDecision
from routes.chat import completions_turn as completions_module


@pytest.fixture
def _stub_turn_deps(monkeypatch):
    """Isolate prepare_agent_turn from DB + prompt-building internals so the test
    asserts ONLY the region-injection behavior."""
    async def fake_prompt(_pid, *, selected_persona_id=None):
        return "BASE AGENT PROMPT", {}

    async def fake_current_doc(_session, _pid, _user, context_ids=None, open_doc_id=None):
        return "# Current document\n- Working in: Doc (id: d-1)\n"

    monkeypatch.setattr(completions_module, "build_prompt_and_skill_docs", fake_prompt)
    monkeypatch.setattr(completions_module, "_build_current_document_section", fake_current_doc)
    # Apply-mode has its own tests; stub it here to decouple.
    monkeypatch.setattr(
        completions_module, "resolve_apply_mode",
        lambda **_kw: ApplyDecision(CONFIRM, "ui_confirm"),
    )


def _ctx():
    return SimpleNamespace(system_prefix=[], image_parts=[])


async def _run(body_region):
    from models import CompletionRequest

    body = CompletionRequest(
        messages=[{"role": "user", "content": "polish this"}],
        region=body_region,
    )
    return await completions_module.prepare_agent_turn(
        session={"project_id": "p-1", "document_id": "d-1", "system_prompt_id": None},
        body=body,
        ctx=_ctx(),
        project_id="p-1",
        user={"user_id": "u-1"},
        toolset=[],
        capability_is_mutating=True,
    )


async def test_prompt_includes_pinned_fragment_when_region_present(_stub_turn_deps):
    plan = await _run({"doc_id": "d-1", "from_cp": 0, "to_cp": 11, "text": "hello world"})
    assert "# Pinned fragment" in plan.system_prompt
    assert "hello world" in plan.system_prompt
    # The hard-scope instruction must reach the model.
    assert "only" in plan.system_prompt.lower()


async def test_prompt_omits_pinned_fragment_when_no_region(_stub_turn_deps):
    plan = await _run(None)
    assert "# Pinned fragment" not in plan.system_prompt
    # The base prompt + current-doc section are still present.
    assert "BASE AGENT PROMPT" in plan.system_prompt


async def test_pinned_fragment_appended_after_current_document(_stub_turn_deps):
    # Ordering: base prompt → context → current document → pinned fragment.
    plan = await _run({"doc_id": "d-1", "from_cp": 0, "to_cp": 5, "text": "hello"})
    assert plan.system_prompt.index("# Current document") < plan.system_prompt.index("# Pinned fragment")
