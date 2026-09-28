"""Round-trip economy of the apply entry — the run key row is read ONCE.

The audit found the api_keys row fetched twice per apply batch (scope resolution +
owner resolution). The contract pinned here: `_resolve_apply_inputs` resolves BOTH the
scope wall and the owning user from a SINGLE api_keys read. The security wall itself
(each request re-reads the key row; scope is never cached across requests) is
deliberate and unaffected — what is pinned is the per-request ROUND TRIP, not the
per-session cache.
"""

import memory.apply as apply_mod
import pytest
from memory._apply_resolution import _resolve_run


class _CountingDb:
    """Fake db counting api_keys reads; other queries return empty results."""

    def __init__(self) -> None:
        self.api_key_reads = 0

    async def query(self, sql: str, *_args, **_kwargs):
        if "api_keys" in sql:
            self.api_key_reads += 1
            return [{
                "project_id": "p1",
                "document_id": "mem-folder",
                "deleted_at": None,
                "user_id": "u1",
            }]
        # portion-source lookup: a valid reference row; gates: no rows needed for []
        if "is_reference = true" in sql:
            return [{"id": "r1"}]
        return []


@pytest.fixture
def counting_db(monkeypatch):
    db = _CountingDb()

    async def fake_get_db():
        return db

    import memory._apply_resolution as resolution_mod

    monkeypatch.setattr(apply_mod, "get_db", fake_get_db)
    monkeypatch.setattr(resolution_mod, "get_db", fake_get_db)
    return db


@pytest.mark.asyncio
async def test_resolve_run_returns_owner_from_the_same_read(counting_db):
    """_resolve_run exposes the run's user alongside the scope root."""
    res = await _resolve_run("p1", "run-1")
    assert res.scope_root == "mem-folder"
    assert res.user_id == "u1"


@pytest.mark.asyncio
async def test_apply_inputs_read_the_key_row_once(counting_db, monkeypatch):
    async def fake_resolve_user(user_id: str):
        return {"user_id": user_id, "name": "owner"}

    import api_key_auth

    monkeypatch.setattr(api_key_auth, "_resolve_user", fake_resolve_user)

    async def no_duplicates(verdicts, *, project_id):
        return {}

    monkeypatch.setattr(apply_mod, "duplicate_fact_errors", no_duplicates, raising=False)

    # dedup is imported inside the function body from memory.dedup — patch it there too
    import memory.dedup

    monkeypatch.setattr(memory.dedup, "duplicate_fact_errors", no_duplicates)

    scope_root, user, refusals = await apply_mod._resolve_apply_inputs(
        project_id="p1", run_id="run-1", reference_id="r1",
        verdicts=[], user=None,
    )
    assert scope_root == "mem-folder"
    assert user == {"user_id": "u1", "name": "owner"}
    assert refusals == {}
    assert counting_db.api_key_reads == 1, (
        "the run key row must be read ONCE per apply batch (scope + owner)"
    )
