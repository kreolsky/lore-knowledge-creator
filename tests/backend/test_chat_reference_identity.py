"""Reference-scoped chat identity + agent-prompt context tests.

Pins two recurring user rules (2026-07-09):

- Bug A: the agent-prompt Parent line must be emitted ONLY when the parent doc is
  in the request context_ids (a lone reference must not drag its owning doc in).
- Bug B: a reference-scoped chat's parent IS the reference — the serializer exposes
  reference_id + reference_title (the ref's OWN title) and never remaps document_id
  to the owning document.
"""
import pytest

# ─── Bug A: agent-prompt Parent-line gate ─────────────────────────────────────


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
