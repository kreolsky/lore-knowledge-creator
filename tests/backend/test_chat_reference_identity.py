"""Reference-scoped chat identity + agent-prompt context tests.

Pins two recurring user rules (2026-07-09):

- Bug A: the agent-prompt Parent line must be emitted ONLY when the parent doc is
  in the request context_ids (a lone reference must not drag its owning doc in).
- Bug B: a reference-scoped chat's parent IS the reference — the serializer exposes
  reference_id + reference_title (the ref's OWN title) and never remaps document_id
  to the owning document.
"""
import pytest
from chat_sessions.serialize import serialize_session


def _ai_row(document_id: str, session_id: str = "chat_sessions:s1") -> dict:
    return {
        "id": session_id,
        "title": "Chat",
        "document_id": document_id,
        "user_id": "users:u1",
        "project_id": "projects:p1",
        "mode": "agent",
        "is_note": False,
        "context_document_ids": [],
        "created_at": None,
    }


# ─── Bug B: reference identity in the serializer ──────────────────────────────

def test_reference_session_keeps_document_id_and_exposes_reference_title():
    """A ref-scoped row exposes reference_id + reference_title (the ref's OWN
    title) and does NOT remap document_id to the owning document."""
    ref_map = {
        "documents:ref1": {
            "is_reference": True,
            "parent_id": "documents:owner1",
            "title": "The Reference Title",
        },
    }
    out = serialize_session(_ai_row("documents:ref1"), ref_map)
    assert out["reference_id"] == "documents:ref1"
    assert out["reference_title"] == "The Reference Title"
    # document_id must NOT be remapped to the owning document.
    assert out["document_id"] == "documents:ref1"
    # A ref-session must not carry a document_title (frontend uses reference_title).
    assert out["document_title"] is None


def test_document_session_has_no_reference_fields():
    """A genuine document-session carries document_title and null ref fields."""
    out = serialize_session(
        _ai_row("documents:doc1"),
        {},
        document_titles={"documents:doc1": "Owning Doc"},
    )
    assert out["reference_id"] is None
    assert out["reference_title"] is None
    assert out["document_id"] == "documents:doc1"
    assert out["document_title"] == "Owning Doc"


# ─── Bug A: agent-prompt Parent-line gate ─────────────────────────────────────

@pytest.mark.asyncio
async def test_parent_line_omitted_when_parent_not_in_context(monkeypatch):
    """Reference-scoped session, parent NOT in context_ids → no Parent line."""
    from routes.chat import completions_turn as completions

    async def fake_fetch_one(_table, doc_id):
        if doc_id == "ref1":
            return {"title": "Ref", "is_reference": True, "parent_id": "owner1"}
        if doc_id == "owner1":
            return {"title": "Owner", "is_reference": False}
        return None

    monkeypatch.setattr(completions, "fetch_one", fake_fetch_one)
    section = await completions._build_current_document_section(
        {"document_id": "ref1"}, "projects:p1", {"user_id": "u1"}, context_ids=[]
    )
    assert section is not None
    assert "- Parent:" not in section


@pytest.mark.asyncio
async def test_parent_line_present_when_parent_in_context(monkeypatch):
    """Split view (parent in context_ids) → Parent line present."""
    from routes.chat import completions_turn as completions

    async def fake_fetch_one(_table, doc_id):
        if doc_id == "ref1":
            return {"title": "Ref", "is_reference": True, "parent_id": "owner1"}
        if doc_id == "owner1":
            return {"title": "Owner", "is_reference": False}
        return None

    async def fake_access(_doc_id, _user):
        return "editor"

    monkeypatch.setattr(completions, "fetch_one", fake_fetch_one)
    import access
    monkeypatch.setattr(access, "get_document_access", fake_access)
    section = await completions._build_current_document_section(
        {"document_id": "ref1"}, "projects:p1", {"user_id": "u1"},
        context_ids=["owner1"],
    )
    # The owning document lands on its OWN bullet, carrying id and title — the
    # label is prose and gets reworded; which fact is revealed is the contract.
    parent = [ln for ln in section.splitlines() if "owner1" in ln]
    assert len(parent) == 1, f"expected one bullet naming the owner, got {parent}"
    assert "Owner" in parent[0]
    assert "ref1" not in parent[0]
