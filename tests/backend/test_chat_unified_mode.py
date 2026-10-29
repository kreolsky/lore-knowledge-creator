"""Unified chat tests — one AI line (agent), apply-time splice, search
normalization, forced-RAG removal.

# SYSTEM: chat-unified-mode-tests — plan: magical-snuggling-diffie (Increment 1)

Unit-level (mirrors the agent-mode test files): pure helpers + monkeypatched
endpoints. The full SSE-loop integration stays a manual E2E per the plan.

plan: remove-ask-line-mode-axis — the Ask-line `mode` axis is gone. The mode-
model + read-time collapse tests were deleted with the wire field; the
tools-for-mode tests were rewritten to agent_toolset() (no mode arg).
"""


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

    # capability-tiers: the tier gate runs BEFORE the access check — give u-1 a
    # real 'ai' row so the REAL gate passes and the access axis stays under test.
    from db import create_record, get_db
    await (await get_db()).query("DELETE type::record('users', $id)", {"id": "u-1"})
    await create_record("users", "u-1", {"name": "u1", "role": "user", "tier": "ai"})

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
    # The note gate (access.can_write_note) resolves the level from the access module.
    monkeypatch.setattr("access.get_document_access", fake_access)
    monkeypatch.setattr("chat_sessions.create.create_record", fake_create_record)
    monkeypatch.setattr(sessions_module, "get_db", fake_get_db)
    monkeypatch.setattr("chat_sessions.serialize.build_ref_map", fake_build_ref_map)

    body = SessionCreate(project_id="proj-1", document_id="doc-1", is_note=True)
    out = await sessions_module.create_session(db=await sessions_module.get_db(), body=body, user={"user_id": "u-1"})
    assert out.get("is_note") is True
