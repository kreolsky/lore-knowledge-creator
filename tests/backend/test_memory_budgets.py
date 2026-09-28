"""Budget contracts of the memory serve/candidates paths.

Pins the "never silently hide a merge candidate / fact" contract on BOTH budgeted
surfaces: `_page_facts_by_budget` (get_memory_facts paging) and
`_attach_candidate_bodies` (merge candidates). Over-budget entries must be NAMED
(`deferred` / `body_deferred`), never dropped or silently truncated — and the FIRST
entry always serves, so a single-item fetch can never come back empty.
"""

import pytest
from memory._candidates import _attach_candidate_bodies
from memory._serve import _page_facts_by_budget

import config


def _fact(doc_id: str, text: str, title: str = "") -> dict:
    return {"id": doc_id, "title": title, "content": text, "mem": {}}


class _FakeDb:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def query(self, _sql, _params=None):
        return self._rows


@pytest.fixture(autouse=True)
def _tiny_budgets(monkeypatch):
    monkeypatch.setattr(config, "MEMORY_FACTS_PAGE_CHARS", 100, raising=False)
    monkeypatch.setattr(config, "MEMORY_MERGE_CANDIDATE_CHARS", 100, raising=False)


class TestPageFactsByBudget:
    def test_first_fact_serves_even_when_alone_over_budget(self):
        big = _fact("f1", "x" * 500)
        res = _page_facts_by_budget(["f1"], {"f1": big})
        assert [f["id"] for f in res["facts"]] == ["f1"]
        assert res["deferred"] == []
        assert res["missing"] == []

    def test_over_budget_fact_is_deferred_by_name_not_dropped(self):
        res = _page_facts_by_budget(
            ["f1", "f2"], {"f1": _fact("f1", "x" * 60), "f2": _fact("f2", "y" * 500, title="T2")},
        )
        assert [f["id"] for f in res["facts"]] == ["f1"]
        assert res["deferred"] == [{"id": "f2", "title": "T2"}]

    def test_missing_ids_are_reported(self):
        res = _page_facts_by_budget(["gone"], {})
        assert res["facts"] == []
        assert res["missing"] == ["gone"]

    def test_within_budget_all_serve_in_order(self):
        res = _page_facts_by_budget(
            ["f1", "f2"], {"f1": _fact("f1", "a"), "f2": _fact("f2", "b")},
        )
        assert [f["id"] for f in res["facts"]] == ["f1", "f2"]
        assert res["deferred"] == []


class TestAttachCandidateBodies:
    @pytest.mark.asyncio
    async def test_first_candidate_always_serves(self):
        db = _FakeDb([{"id": "c1", "content": "x" * 500}])
        out = await _attach_candidate_bodies(db, "p", [{"id": "c1", "title": "T", "score": 0.9}])
        assert out[0]["text"] == "x" * 500
        assert "body_deferred" not in out[0]

    @pytest.mark.asyncio
    async def test_over_budget_candidate_is_marked_body_deferred_keeps_identity(self):
        db = _FakeDb([
            {"id": "c1", "content": "x" * 60},
            {"id": "c2", "content": "y" * 500},
        ])
        out = await _attach_candidate_bodies(db, "p", [
            {"id": "c1", "title": "T1", "score": 0.9},
            {"id": "c2", "title": "T2", "score": 0.8},
        ])
        assert out[0]["text"] == "x" * 60
        assert out[1]["text"] == ""
        assert out[1]["body_deferred"] is True
        # never silently hidden: id + title + score survive the deferral
        assert out[1]["id"] == "c2"
        assert out[1]["title"] == "T2"
        assert out[1]["score"] == 0.8

    @pytest.mark.asyncio
    async def test_within_budget_all_carry_bodies(self):
        db = _FakeDb([{"id": "c1", "content": "a"}, {"id": "c2", "content": "b"}])
        out = await _attach_candidate_bodies(db, "p", [
            {"id": "c1", "title": "T1", "score": 0.9},
            {"id": "c2", "title": "T2", "score": 0.8},
        ])
        assert [c["text"] for c in out] == ["a", "b"]
        assert all("body_deferred" not in c for c in out)

    @pytest.mark.asyncio
    async def test_empty_candidates_short_circuit(self):
        out = await _attach_candidate_bodies(_FakeDb([]), "p", [])
        assert out == []
