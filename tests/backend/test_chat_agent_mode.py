"""Tests for agent-mode chat: tool descriptions, _splice_edit, system-prompt config.

# SYSTEM: chat-agent-mode-tests — unit tests for the surviving agent primitives.

These tests are unit-level by design: they exercise the pure helpers that back
the SSE tool-call branch and the direct apply path (create_document_via_collab /
collab_writes since plan fewer-layers). Full integration (TestClient + DB) is covered by manual E2E
checks per the plan's Verification section.
"""

import pytest
from agent.edit_primitives import _splice_edit
from agent.tools import AGENT_TOOLS

# ─── AGENT_TOOLS description contract (audit fix #3 — anti-loop guidance) ────


def _tool_description(name: str) -> str:
    for t in AGENT_TOOLS:
        if t["function"]["name"] == name:
            return t["function"]["description"]
    raise KeyError(name)


def test_edit_document_description_states_confirmation_hold():
    """The edit_document tool description states a confirm-mode call is HELD for the
    user's approval (the mid-turn approval behavior that replaced the `proposed`
    result — the result-type signal the Pi loop reads off the tool)."""
    desc = _tool_description("edit_document").lower()
    assert "held for the user's approval" in desc


def test_create_document_description_states_confirmation_hold():
    desc = _tool_description("create_document").lower()
    assert "held for the user's approval" in desc


# ─── _splice_edit — proposal apply core ─────────────────────────────────────


def test_splice_replaces_range_when_original_matches():
    content = "Hello cruel world"
    new_content, err = _splice_edit(
        content=content, from_cp=6, to_cp=11,
        original_text="cruel", new_text="beautiful",
    )
    assert err is None
    assert new_content == "Hello beautiful world"


def test_splice_returns_stale_when_original_mismatch():
    content = "Hello cruel world"
    _, err = _splice_edit(
        content=content, from_cp=6, to_cp=11,
        original_text="merry", new_text="beautiful",
    )
    assert err == "stale"


def test_splice_handles_emoji_correctly():
    # "Hi 😀 there" — emoji is one code point, two UTF-16 code units
    content = "Hi 😀 there"
    # Replace "😀" — code points 3..4
    new_content, err = _splice_edit(
        content=content, from_cp=3, to_cp=4,
        original_text="😀", new_text="🌟",
    )
    assert err is None
    assert new_content == "Hi 🌟 there"


def test_splice_rejects_out_of_bounds():
    content = "short"
    _, err = _splice_edit(
        content=content, from_cp=0, to_cp=999,
        original_text="short", new_text="x",
    )
    assert err == "stale"


def test_splice_rejects_inverted_range():
    content = "hello"
    _, err = _splice_edit(
        content=content, from_cp=3, to_cp=1,
        original_text="el", new_text="x",
    )
    assert err == "stale"


# ─── System prompt config ───────────────────────────────────────────────────
# plan: remove-ask-line-mode-axis (audit §B): test_agent_system_prompt_loaded_
# and_non_empty asserted on PROMPT_CHAT_AGENT_SYSTEM_PROMPT — a dead prompt that
# never reached an agent turn (the live prompt is the AGENT_BOOTSTRAP_PROMPT setting, in
# agent_config.py). Deleted with the constant (D7).


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

    body = SessionCreate(
        project_id="proj-1",
        document_id="doc-1",
    )
    user = {"user_id": "u-1"}

    with pytest.raises(HTTPException) as exc:
        await sessions_module.create_session(db=await sessions_module.get_db(), body=body, user=user)
    assert exc.value.status_code == 403
    assert "agent mode" in exc.value.detail.lower()


async def test_create_session_allows_ai_chat_for_full_access(monkeypatch):
    """Full-access users can create an AI chat (every AI chat is an agent chat).
    plan: remove-ask-line-mode-axis — `mode` is no longer on the wire; target_doc_id
    is now written unconditionally for non-note sessions."""
    from routes.chat import sessions as sessions_module

    from models import SessionCreate

    async def fake_access(_did, _user):
        return "full"

    async def fake_resolve_inherited(_db, _body, _uid):
        return {"model": "m", "system_prompt_id": None}

    async def fake_create_record(_table, rid, payload):
        return {"id": f"chat_sessions:{rid}", **payload}

    class _FakeDB:
        async def query(self, _q, _p=None):
            return []

    async def fake_get_db():
        return _FakeDB()

    async def fake_build_ref_map(_db, *_ids):
        return {}

    monkeypatch.setattr(sessions_module, "get_document_access", fake_access)
    monkeypatch.setattr("chat_sessions.create._resolve_inherited", fake_resolve_inherited)
    monkeypatch.setattr("chat_sessions.create.create_record", fake_create_record)
    monkeypatch.setattr(sessions_module, "get_db", fake_get_db)
    monkeypatch.setattr("chat_sessions.serialize.build_ref_map", fake_build_ref_map)

    body = SessionCreate(
        project_id="proj-1",
        document_id="doc-1",
    )
    out = await sessions_module.create_session(db=await sessions_module.get_db(), body=body, user={"user_id": "u-1"})
    # D1: no `mode` key on the wire; target_doc_id written unconditionally (D3).
    assert "mode" not in out
    assert out["target_doc_id"] == "doc-1"


# ─── S2: MAX_PROPOSAL_NEW_TEXT_CHARS guard ─────────────────────────────────


def test_max_proposal_new_text_chars_is_configured():
    """Constant exists, is a positive int, and is larger than original_text cap (50k)."""
    from config import MAX_PROPOSAL_NEW_TEXT_CHARS

    assert isinstance(MAX_PROPOSAL_NEW_TEXT_CHARS, int)
    assert MAX_PROPOSAL_NEW_TEXT_CHARS > 50_000
    # Sanity: not absurdly large
    assert MAX_PROPOSAL_NEW_TEXT_CHARS <= 10_000_000


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


# ─── List spacing normalization at apply time ───────────────────────────────


async def test_apply_create_normalizes_list_spacing_in_content(monkeypatch, emit_recorder):
    """A create proposal's content is normalized before the document is written.

    PR4 R3 consolidated the create body (incl. list-spacing normalization) into the
    single `create_document_via_collab` primitive shared by the Tool-API direct
    path and the chat proposal apply path. This test targets that primitive directly
    so the two entry points can never drift on the invariant."""
    from agent.collab_writes import create_document_via_collab

    captured: dict = {}
    raw = "*   [ ] one\n-   two"

    async def fake_assert_parent(_parent_id, _project_id):
        return None

    async def fake_create_unique(doc_id, payload, *, project_id, title,
                                 user_id=None, user_name=None):
        captured["created_content"] = payload["content"]
        payload["path"] = "new-doc.md"
        payload.setdefault("sort_key", 0)

    import documents.service as docs_module
    monkeypatch.setattr(docs_module, "assert_parent_valid", fake_assert_parent)
    monkeypatch.setattr(docs_module, "create_with_unique_path", fake_create_unique)
    await create_document_via_collab(
        title="New Doc", content=raw, parent_id=None,
        project_id="p-1", user={"user_id": "u-1"},
    )
    stored = captured["created_content"]
    assert "*   [ ]" not in stored
    assert "* [ ] one" in stored
    assert "- two" in stored
