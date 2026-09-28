"""Unified chat tests — one AI line (agent), apply-time splice, search
normalization, forced-RAG removal.

# SYSTEM: chat-unified-mode-tests — plan: magical-snuggling-diffie (Increment 1)

Unit-level (mirrors the agent-mode test files): pure helpers + monkeypatched
endpoints. The full SSE-loop integration stays a manual E2E per the plan.

plan: remove-ask-line-mode-axis — the Ask-line `mode` axis is gone. The mode-
model + read-time collapse tests were deleted with the wire field; the
tools-for-mode tests were rewritten to agent_toolset() (no mode arg).
"""

import pytest

# ─── §1 One AI line — the tool surface is always the full agent set ─────────


@pytest.fixture
def _tool_axes_off(monkeypatch):
    """Pin every conditional append OFF so this file tests the base surface alone.

    agent_toolset appends env-gated tools — the agent-sandbox console and the
    ComfyUI image generator — gated on their BASE
    keys (SANDBOX_SSH_HOST+KEY, COMFYUI_URL) re-derived through
    settings at call time, which makes the surface env-dependent: a dev .env
    with any of those set would otherwise change the result of these tests.
    The ON surfaces are covered by their own files (test_sandbox_tool.py,
    the image-gen tests); here we assert the base surface, so we
    pin the other axes rather than loosen the assertion.

    INVARIANT: every gate branch in agent_toolset must be pinned here. Why: an
    unpinned gate makes these equality assertions pass or fail on the runner's
    .env, not on the code — both new gates (COMFYUI, SEARCH, run_extractor's
    MCP twin) reached dev red exactly this way.
    """
    from helpers import pin_tool_gates

    pin_tool_gates(monkeypatch, sandbox=False, comfy=False)


async def test_agent_toolset_returns_full_surface(_tool_axes_off):
    # Stage 9 (Pi is the only line): every AI chat is an agent chat, so
    # agent_toolset returns the full AGENT_TOOLS surface. There is no mode arg
    # anymore — the Ask zero-tools line is deleted (plan: remove-ask-line-mode-axis).
    # reprocess_reference is appended UNCONDITIONALLY (no external dependency, unlike
    # the sandbox/comfy env-gated tools), so it is part of the base surface here too.
    # consolidate_memory joins it on the same terms — Pi-only for a different reason
    # (it mints a scoped credential, see its ARCH note), but likewise ungated.
    # use_skill is GONE entirely (plan collapse-the-editor-harness-layer step 3):
    # skill bodies ride the payload's raw wire, dsh's `skill` tool is the only loader.
    from agent.tools import AGENT_TOOLS, agent_toolset
    from agent_tools.specs.media import (
        IMPORT_FILE_TOOL,
        REPROCESS_REFERENCE_TOOL,
    )
    from agent_tools.specs.memory import (
        APPLY_MEMORY_VERDICTS_TOOL,
        CONSOLIDATE_MEMORY_TOOL,
        NEXT_REFERENCE_TOOL,
        REOPEN_CONSOLIDATION_TOOL,
    )
    from agent_tools.specs.read import GET_FACT_HISTORY_TOOL, GET_MEMORY_FACTS_TOOL
    from agent_tools.specs.skills import SAVE_SKILL_TOOL
    assert await agent_toolset() == AGENT_TOOLS + [
        REPROCESS_REFERENCE_TOOL, IMPORT_FILE_TOOL,
        CONSOLIDATE_MEMORY_TOOL, NEXT_REFERENCE_TOOL,
        GET_MEMORY_FACTS_TOOL, GET_FACT_HISTORY_TOOL,
        APPLY_MEMORY_VERDICTS_TOOL,
        REOPEN_CONSOLIDATION_TOOL,
        # save_skill (skill-authoring-skill): ungated like reprocess — no
        # external dependency; CORE so a model that skipped the skill load
        # still finds it by name.
        SAVE_SKILL_TOOL,
    ]


async def test_agent_toolset_names_match_agent_tools(_tool_axes_off):
    from agent.tools import AGENT_TOOLS, agent_toolset
    names = {t["function"]["name"] for t in await agent_toolset()}
    assert names == {t["function"]["name"] for t in AGENT_TOOLS} | {
        "reprocess_reference", "import_file", "consolidate_memory",
        "next_reference", "get_memory_facts", "get_fact_history",
        "apply_memory_verdicts", "reopen_consolidation",
        "save_skill",
    }
    # One skill loader only: use_skill is deleted, dsh's `skill` tool is the
    # model-facing loader (the lore-skills provider serves bodies in memory).
    assert "use_skill" not in names


async def test_sandbox_appends_console_to_the_surface(monkeypatch):
    """The sandbox console is appended to the surface when configured — the seam
    where the Pi path (agent_toolset) diverges from the MCP gateway (which derives
    from AGENT_TOOLS and never carries it)."""
    from agent import tools
    from helpers import pin_tool_gates

    pin_tool_gates(monkeypatch, sandbox=True)
    names = {t["function"]["name"] for t in await tools.agent_toolset()}
    assert "sandbox_bash" in names


# ─── §1 creation gating: every AI chat is an agent chat → full access required ───


async def test_create_ai_chat_non_full_user_is_rejected(monkeypatch):
    """Stage 9 backend half (C1): every AI chat is an agent chat, so a non-full user
    cannot create ONE — the create gate is unconditional for AI chats. A readonly-
    access user POSTing /sessions is rejected with 403, closing the direct-API
    creation gap. The frontend already blocks this; this is the backend half
    (defense-in-depth)."""
    import pytest as _pytest
    from fastapi import HTTPException
    from routes.chat import sessions as sessions_module

    from models import SessionCreate

    async def fake_access(_did, _user):
        return "readonly"

    monkeypatch.setattr(sessions_module, "get_document_access", fake_access)

    body = SessionCreate(project_id="proj-1", document_id="doc-1")
    with _pytest.raises(HTTPException) as exc:
        await sessions_module.create_session(db=await sessions_module.get_db(), body=body, user={"user_id": "u-1"})
    assert exc.value.status_code == 403
    assert exc.value.detail == "Full project access required for agent mode"


async def test_create_note_chat_commentator_still_allowed(monkeypatch):
    """The unconditional agent gate must NOT touch the note path: notes are
    LLM-disabled, so a commentator can still create a note-chat."""
    from routes.chat import sessions as sessions_module

    from models import SessionCreate

    async def fake_access(_did, _user):
        return "commentator"

    async def fake_create_record(_table, rid, payload):
        return {"id": f"chat_sessions:{rid}", **payload}

    async def fake_get_db():
        return None

    async def fake_build_ref_map(_db, *_ids):
        return {}

    monkeypatch.setattr(sessions_module, "get_document_access", fake_access)
    monkeypatch.setattr("chat_sessions.create.create_record", fake_create_record)
    monkeypatch.setattr(sessions_module, "get_db", fake_get_db)
    monkeypatch.setattr("chat_sessions.serialize.build_ref_map", fake_build_ref_map)

    body = SessionCreate(project_id="proj-1", document_id="doc-1", is_note=True)
    out = await sessions_module.create_session(db=await sessions_module.get_db(), body=body, user={"user_id": "u-1"})
    assert out.get("is_note") is True


# ─── §4 Search normalization — history threaded ────────────────────────────


async def test_search_threads_history_into_retrieve_context(monkeypatch):
    """Non-empty history reaches retrieve_context (so _rewrite_query can fire)."""
    import types as _types

    # _search_materials_exec moved to search_exec (plan: chat-large-file-decomposition);
    # its call-time deps (get_db / get_document_access) resolve off search_exec.
    from agent import search_exec as ro

    captured: dict = {}

    class _FakeDB:
        async def query(self, _q, _p):
            return []

    async def fake_get_db():
        return _FakeDB()

    async def fake_access(_d, _u):
        return "full"

    async def fake_retrieve(**kw):
        captured.update(kw)
        return _types.SimpleNamespace(error=None, hits=[])

    monkeypatch.setattr(ro, "get_db", fake_get_db)
    monkeypatch.setattr(ro, "get_document_access", fake_access)
    import retrieval
    monkeypatch.setattr(retrieval, "retrieve_context", fake_retrieve)

    await ro._search_materials_exec(
        project_id="p-1", user={"user_id": "u-1"}, query="dragons",
        history=[{"role": "user", "content": "tell me about dragons in the north"}],
    )
    assert captured["history"] == [{"role": "user", "content": "tell me about dragons in the north"}]


async def test_search_default_history_empty(monkeypatch):
    """Back-compat: omitting history passes an empty list (raw query)."""
    import types as _types

    # _search_materials_exec moved to search_exec (plan: chat-large-file-decomposition);
    # its call-time deps (get_db / get_document_access) resolve off search_exec.
    from agent import search_exec as ro

    captured: dict = {}

    class _FakeDB:
        async def query(self, _q, _p):
            return []

    async def fake_get_db():
        return _FakeDB()

    async def fake_access(_d, _u):
        return "full"

    async def fake_retrieve(**kw):
        captured.update(kw)
        return _types.SimpleNamespace(error=None, hits=[])

    monkeypatch.setattr(ro, "get_db", fake_get_db)
    monkeypatch.setattr(ro, "get_document_access", fake_access)
    import retrieval
    monkeypatch.setattr(retrieval, "retrieve_context", fake_retrieve)

    await ro._search_materials_exec(
        project_id="p-1", user={"user_id": "u-1"}, query="x",
    )
    assert captured["history"] == []


# ─── §5 Prompt rewrite ──────────────────────────────────────────────────────
# plan: remove-ask-line-mode-axis (audit §B): the two tests that asserted on
# PROMPT_CHAT_AGENT_MULTI_TOOL_SYSTEM_PROMPT were "tests that bind nothing" —
# that prompt never reached an agent turn (the live prompt is
# BOOTSTRAP_SYSTEM_PROMPT in agent_config.py). Deleted with the constant (D7).

