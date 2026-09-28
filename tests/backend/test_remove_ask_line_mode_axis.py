"""TDD failing tests for plan: remove-ask-line-mode-axis.

Pins the NEW wire/behavior contract after the Ask-line `mode` axis is deleted:

  D1 — `mode` leaves the wire: create without `mode` round-trips
       target_doc_id / agent_auto / has_region AND the serialized output
       carries NO `mode` key.
  D3 — agent-only create fields become unconditional for non-note sessions
       (target_doc_id / agent_auto / has_region written for every AI chat).
  D4 — target_doc_id defaulting is RELOCATED (not lost): omitted →
       reference_id or document_id for a non-note session; a note session with
       has_region=true is still rejected.
  D5 — _normalize_mode is deleted outright (no wire field + no reader).
  D9 — the always-true functional mode reads are REWRITTEN to the is_note axis
       (sessions.py agent_auto PATCH gate). The context.py agent_target read
       (D9) is a code-clarity rewrite with NO observable behavior change
       (the two expressions are equivalent for every session shape) — it is
       verified by the Order-step-6 grep gate + review, not a unit test here.

These fail against the current (pre-removal) code for the right reasons, then
turn green once the removal lands.
"""
import pytest
from pydantic import ValidationError

from models import SessionCreate

# ─── D1 / D5 — wire shape + helper removal ────────────────────────────────────


def test_session_create_has_no_mode_field():
    """D1: `mode` is removed from the SessionCreate model entirely."""
    fields = set(SessionCreate.model_fields.keys())
    assert "mode" not in fields, (
        f"SessionCreate must not carry a `mode` field after the Ask-line removal; "
        f"got fields={sorted(fields)}"
    )


def test_session_update_has_no_mode_field():
    """D1: `mode` is removed from SessionUpdate (the PATCH-mode branch leaves)."""
    from models import SessionUpdate
    fields = set(SessionUpdate.model_fields.keys())
    assert "mode" not in fields


def test_serialize_session_emits_no_mode_key():
    """D1: the serialized session output carries NO `mode` key at all."""
    from chat_sessions.serialize import serialize_session
    out = serialize_session({"id": "chat_sessions:s1", "mode": "agent"})
    assert "mode" not in out


def test_normalize_mode_is_deleted():
    """D5: _normalize_mode (the read-time collapse helper) is removed outright."""
    from chat_sessions import serialize as serializers
    assert not hasattr(serializers, "_normalize_mode"), (
        "_normalize_mode must be deleted (no wire field + no reader -> defends nothing)"
    )


# ─── D1 / D3 — create round-trips the agent-only fields unconditionally ─────


async def test_create_ai_chat_writes_target_doc_and_agent_auto_unconditionally(monkeypatch):
    """D3: target_doc_id / agent_auto / has_region are written for EVERY non-note
    session, not only when mode='agent'. Captures the create_record payload and
    asserts the three fields land on the row (with target_doc_id defaulted per D4)."""
    from routes.chat import sessions as sessions_module

    captured: dict = {}

    async def fake_access(_did, _user):
        return "full"

    async def fake_create_record(_table, rid, payload):
        captured.update(payload)
        return {"id": f"chat_sessions:{rid}", **payload}

    class _FakeDB:
        # _resolve_inherited (project-latest model/system_prompt walk) reads here.
        async def query(self, _q, _p=None):
            return []

    async def fake_get_db():
        return _FakeDB()

    async def fake_build_ref_map(_db, *_ids):
        return {}

    monkeypatch.setattr(sessions_module, "get_document_access", fake_access)
    monkeypatch.setattr("chat_sessions.create.create_record", fake_create_record)
    monkeypatch.setattr(sessions_module, "get_db", fake_get_db)
    monkeypatch.setattr("chat_sessions.serialize.build_ref_map", fake_build_ref_map)

    # No `mode`, no explicit target_doc_id — the D4 default must still resolve it.
    body = SessionCreate(project_id="proj-1", document_id="doc-1")
    out = await sessions_module.create_session(db=await sessions_module.get_db(), body=body, user={"user_id": "u-1"})

    # D3: all three agent-only fields persisted for a non-note session.
    assert captured.get("target_doc_id") == "doc-1"
    assert captured.get("agent_auto") is False
    assert captured.get("has_region") is False
    # D1: the persisted row never carries a `mode` column write from this route.
    assert "mode" not in captured
    # D1: the serialized response carries no `mode` key.
    assert "mode" not in out


# ─── D4 — target_doc_id defaulting RELOCATED + note incompatibility ──────────


def test_target_doc_id_defaults_to_reference_or_document_for_non_note():
    """D4(1): create WITHOUT an explicit target_doc_id defaults it to
    reference_id or document_id. Previously this ran only inside
    `if self.mode == 'agent'`; dropping that block naively would leave
    target_doc_id=None -> agent has no pinned document."""
    s = SessionCreate(project_id="p-1", document_id="doc-1")
    assert s.target_doc_id == "doc-1"

    s_ref = SessionCreate(project_id="p-1", reference_id="ref-1")
    assert s_ref.target_doc_id == "ref-1"

    # Explicit value is honored (not overwritten by the default).
    s_explicit = SessionCreate(project_id="p-1", document_id="doc-1", target_doc_id="pinned-1")
    assert s_explicit.target_doc_id == "pinned-1"


def test_note_session_with_has_region_is_rejected():
    """D4(3): the note-incompatibility rule is RE-EXPRESSED on the is_note axis.
    A note session carrying has_region=true must be rejected (a pinned region
    only makes sense for an agent that edits documents)."""
    with pytest.raises(ValidationError) as exc:
        SessionCreate(
            project_id="p-1", document_id="doc-1", is_note=True, has_region=True,
        )
    msg = str(exc.value).lower()
    assert "has_region" in msg
    assert "note" in msg


def test_non_note_has_region_is_accepted():
    """D4(2): the old `has_region requires agent mode` guard is deleted; a
    non-note session with has_region=true is accepted (every AI chat is agent)."""
    s = SessionCreate(
        project_id="p-1", document_id="doc-1", has_region=True,
    )
    assert s.has_region is True


# ─── D9 — PATCH agent_auto gate rewritten to is_note axis ────────────────────


async def test_patch_agent_auto_blocked_for_note_session(monkeypatch):
    """D9: the PATCH `agent_auto` branch's `can_auto` gate reads the is_note axis,
    not the mode axis. A note session PATCHing agent_auto=true is forced false
    (notes never carry an auto-apply grant) — defense-in-depth.

    Against the current code this FAILS: _normalize_mode collapses the note's
    legacy mode to 'agent', so can_auto=True and agent_auto=true is persisted."""
    from routes.chat import sessions as sessions_module

    from models import SessionUpdate

    async def fake_require_session_access(_sid, _user):
        # A NOTE session: is_note=True. Its `mode` column may carry a legacy
        # value — the gate must read is_note, not mode.
        return {"session_id": "s-note", "is_note": True, "mode": "chat",
                "project_id": "p-1", "document_id": "d-1"}

    async def fake_resolve_scope_access(_session, _user):
        return "full"

    written: dict = {}

    class _FakeDB:
        async def query(self, _q, params=None):
            # Reflect what the route actually wrote so the test observes the gate.
            written.update(params)
            return [{
                "id": "chat_sessions:s-note", "session_id": "s-note",
                "is_note": True, "document_id": "d-1",
                "agent_auto": bool(params.get("agent_auto")),
            }]

    async def fake_get_db():
        return _FakeDB()

    async def fake_build_ref_map(_db, *_ids):
        return {}

    monkeypatch.setattr(sessions_module, "_require_session_access", fake_require_session_access)
    monkeypatch.setattr("chat_sessions.update._resolve_session_scope_access", fake_resolve_scope_access)
    monkeypatch.setattr(sessions_module, "get_db", fake_get_db)
    monkeypatch.setattr("chat_sessions.serialize.build_ref_map", fake_build_ref_map)

    body = SessionUpdate(agent_auto=True)
    out = await sessions_module.update_session(db=await sessions_module.get_db(), 
        session_id="s-note", body=body, user={"user_id": "u-1"},
    )
    # The gate forced agent_auto=false despite the request and full access.
    assert written.get("agent_auto") is False
    assert out["agent_auto"] is False
