"""Tests for agent-mode chat: tool descriptions, _splice_edit, system-prompt config.

# SYSTEM: chat-agent-mode-tests — unit tests for the surviving agent primitives.

These tests are unit-level by design: they exercise the pure helpers that back
the SSE tool-call branch and the direct apply path (create_document_via_collab /
collab_writes since plan fewer-layers). Full integration (TestClient + DB) is covered by manual E2E
checks per the plan's Verification section.
"""

import pytest
from agent.edit_primitives import _splice_edit

# ─── _splice_edit — proposal apply core ─────────────────────────────────────


def test_splice_returns_stale_when_original_mismatch():
    content = "Hello cruel world"
    _, err = _splice_edit(
        content=content, from_cp=6, to_cp=11,
        original_text="merry", new_text="beautiful",
    )
    assert err == "stale"


def test_splice_rejects_out_of_bounds():
    content = "short"
    _, err = _splice_edit(
        content=content, from_cp=0, to_cp=999,
        original_text="short", new_text="x",
    )
    assert err == "stale"


# ─── _create_agent_pre_edit_checkpoint — auto-snapshot before agent apply ───


async def test_agent_pre_edit_checkpoint_persists_with_expected_label(monkeypatch, emit_recorder):
    """Helper routes through the unified writer with label='agent-auto' + pre-edit content.

    Retargeted to the cp_store.create_checkpoint boundary (the old create_record/
    serialize_record internals moved into the writer with this change). Behavioral
    assertions — label, content, comment, checkpoint_created emission — are unchanged.
    """
    from agent import collab_writes as agent_module

    captured: dict = {}

    async def fake_create_checkpoint(*, document_id, content, tables_json, label, comment, created_by):
        captured["kwargs"] = {
            "document_id": document_id, "content": content, "tables_json": tables_json,
            "label": label, "comment": comment, "created_by": created_by,
        }
        return {"checkpoint_id": "cp-1", "label": label, "content": content}

    monkeypatch.setattr(agent_module, "create_checkpoint", fake_create_checkpoint, raising=False)
    # create_checkpoint is imported lazily inside the helper; patch it at its source too.
    import cp_store
    monkeypatch.setattr(cp_store, "create_checkpoint", fake_create_checkpoint)

    import deps
    monkeypatch.setattr(deps, "json_safe", lambda x: x)

    result = await agent_module._create_agent_pre_edit_checkpoint(
        document_id="doc-42",
        content="Hello world",
        tables_json="{}",
        original_preview="Hello",
    )

    assert captured["kwargs"]["document_id"] == "doc-42"
    assert captured["kwargs"]["content"] == "Hello world"
    assert captured["kwargs"]["label"] == "agent-auto"
    assert "Before agent edit: 'Hello'" in captured["kwargs"]["comment"]

    # The 'checkpoint_created' event must fire so HistoryPanel updates live.
    assert "checkpoint_created" in emit_recorder.names()

    assert result is not None
    assert result["label"] == "agent-auto"


# ─── S1: create_session gates mode='agent' on access='full' ────────────────


async def test_create_session_rejects_agent_mode_for_non_full_access(monkeypatch):
    """Commentator / viewer cannot open an agent session, even if Apply is also gated."""
    from fastapi import HTTPException
    from routes.chat import sessions as sessions_module

    from models import SessionCreate

    async def fake_access(_did, _user):
        return "commentator"

    monkeypatch.setattr(sessions_module, "get_document_access", fake_access)

    # capability-tiers: the tier gate runs BEFORE the access check — give u-1 a
    # real 'ai' row so the REAL gate passes and the access axis stays under test.
    from db import create_record, get_db
    await (await get_db()).query("DELETE type::record('users', $id)", {"id": "u-1"})
    await create_record("users", "u-1", {"name": "u1", "role": "user", "tier": "ai"})

    body = SessionCreate(
        project_id="proj-1",
        document_id="doc-1",
    )
    user = {"user_id": "u-1"}

    with pytest.raises(HTTPException) as exc:
        await sessions_module.create_session(db=await sessions_module.get_db(), body=body, user=user)
    assert exc.value.status_code == 403
    assert "agent mode" in exc.value.detail.lower()


# ─── S2: MAX_PROPOSAL_NEW_TEXT_CHARS guard ─────────────────────────────────


async def test_agent_pre_edit_checkpoint_propagates_writer_failure(monkeypatch, emit_recorder):
    """If the unified writer fails to persist, the helper propagates and does not emit.

    The old None-return-on-vanish path moved into cp_store.create_checkpoint (which
    raises); the helper is now a thin pass-through, so a writer failure surfaces rather
    than silently returning None (no silent degradation).
    """
    from agent import collab_writes as agent_module

    async def fake_create_checkpoint(**_kwargs):
        raise RuntimeError("writer failure")

    import cp_store
    monkeypatch.setattr(cp_store, "create_checkpoint", fake_create_checkpoint)

    import deps
    monkeypatch.setattr(deps, "json_safe", lambda x: x)

    with pytest.raises(RuntimeError, match="writer failure"):
        await agent_module._create_agent_pre_edit_checkpoint(
            document_id="doc-x",
            content="anything",
            tables_json="{}",
            original_preview="prev",
        )
    # Must not emit when the row didn't actually land.
    assert emit_recorder.calls == []
