"""M7: gate_mutation_target — the shared fetch+RBAC+scope gate for scoped mutation
entry points. "Test the principal that must be REFUSED" — exercise the guard with a
caller that would otherwise bypass it (non-full access, out-of-scope, cross-project,
deleted)."""
import pytest
from fastapi import HTTPException

import scope as scope_mod


def _wire(monkeypatch, *, doc, access, in_scope=True):
    async def fake_fetch_one(_table, _id):
        return doc

    async def fake_access(_did, _user):
        return access

    async def fake_in_subtree(_root, _did):
        return in_scope

    monkeypatch.setattr("db.fetch_one", fake_fetch_one)
    monkeypatch.setattr("access.get_document_access", fake_access)
    monkeypatch.setattr(scope_mod, "in_subtree", fake_in_subtree)


_USER = {"user_id": "u1"}
_DOC = {"id": "documents:d1", "project_id": "p1", "deleted_at": None}


async def test_full_access_in_scope_passes(monkeypatch):
    _wire(monkeypatch, doc=_DOC, access="full", in_scope=True)
    row, access = await scope_mod.gate_mutation_target(
        user=_USER, doc_id="d1", project_id="p1", scope_root="root",
    )
    assert row["project_id"] == "p1"
    assert access == "full"


async def test_non_full_access_is_refused_403(monkeypatch):
    """The principal that must be refused: a commentator reaching a mutation gate."""
    _wire(monkeypatch, doc=_DOC, access="commentator", in_scope=True)
    with pytest.raises(HTTPException) as exc:
        await scope_mod.gate_mutation_target(
            user=_USER, doc_id="d1", project_id="p1", scope_root="root",
        )
    assert exc.value.status_code == 403


async def test_out_of_scope_is_refused_403(monkeypatch):
    """Full access but outside the key's subtree → 403 (scope ⊂ RBAC)."""
    _wire(monkeypatch, doc=_DOC, access="full", in_scope=False)
    with pytest.raises(HTTPException) as exc:
        await scope_mod.gate_mutation_target(
            user=_USER, doc_id="d1", project_id="p1", scope_root="root",
        )
    assert exc.value.status_code == 403


async def test_cross_project_is_404(monkeypatch):
    _wire(monkeypatch, doc={"project_id": "OTHER", "deleted_at": None}, access="full")
    with pytest.raises(HTTPException) as exc:
        await scope_mod.gate_mutation_target(
            user=_USER, doc_id="d1", project_id="p1", scope_root="root",
        )
    assert exc.value.status_code == 404


async def test_deleted_doc_is_404(monkeypatch):
    _wire(monkeypatch, doc={"project_id": "p1", "deleted_at": "2026-01-01"}, access="full")
    with pytest.raises(HTTPException) as exc:
        await scope_mod.gate_mutation_target(
            user=_USER, doc_id="d1", project_id="p1", scope_root="root",
        )
    assert exc.value.status_code == 404


async def test_missing_doc_is_404(monkeypatch):
    _wire(monkeypatch, doc=None, access="full")
    with pytest.raises(HTTPException) as exc:
        await scope_mod.gate_mutation_target(
            user=_USER, doc_id="d1", project_id="p1", scope_root="root",
        )
    assert exc.value.status_code == 404
