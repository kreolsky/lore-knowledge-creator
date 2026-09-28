"""D8 — references are the journal; the knowledge base is rebuildable from them.

Plan `.kilo/plans/1786230000000-memory-flatten-to-facts.md` D8. Every fact is stamped
with the `reference_id` it came from (`mem.provenance.sources`), references are
immutable, and consolidation is re-runnable over them. That is what licenses "drop and
rebuild" as a migration strategy. These tests pin the stamping and the recoverability
property: every live fact points at a real reference, and the reference graph is
intact enough to re-derive the facts.
"""

import pytest
import pytest_asyncio
from memory_ref import ensure_memory_reference

PID = "test-project-001"


@pytest_asyncio.fixture(autouse=True)
async def _clean_memory_space(test_db):
    await test_db.query(
        "DELETE documents WHERE project_id = $pid AND is_memory = true",
        {"pid": PID},
    )


async def _run(pid, uid):
    from memory.run_key import mint_memory_run_key

    return await mint_memory_run_key(user_id=uid, project_id=pid)


async def _apply(pid, uid, verdicts, portion=""):
    from memory.apply import apply_memory_verdicts

    run = await _run(pid, uid)
    return await apply_memory_verdicts(
        project_id=pid, run_id=run.run_id, verdicts=verdicts,
        reference_id=await ensure_memory_reference(pid, portion),
    )


async def test_every_fact_carries_the_reference_it_came_from(project_with_doc):
    """Provenance is STAMPED from the validated portion, never taken from the verdict.
    A fact's `mem.provenance.sources` names the reference that spawned it."""
    pid, _idx, uid = project_with_doc
    ref_id = await ensure_memory_reference(pid)
    res = await _apply(pid, uid, [{"action": "new", "title": "Т: ф", "text": "x"}])
    from db import get_db

    db = await get_db()
    fact = (await db.query(
        "SELECT mem FROM type::record('documents', $id)", {"id": res["created"][0]},
    ))[0]
    sources = fact["mem"]["provenance"]["sources"]
    assert sources == [{"kind": "reference", "id": ref_id}]


async def test_the_agent_cannot_supply_its_own_sources(project_with_doc):
    """A `sources` field on the verdict is overwritten by the server's stamp —
    provenance is never agent-authored."""
    pid, _idx, uid = project_with_doc
    ref_id = await ensure_memory_reference(pid)
    res = await _apply(pid, uid, [
        {"action": "new", "title": "Т: ф", "text": "x",
         "sources": [{"kind": "run", "id": "fabricated"}]},
    ])
    from db import get_db

    db = await get_db()
    fact = (await db.query(
        "SELECT mem FROM type::record('documents', $id)", {"id": res["created"][0]},
    ))[0]
    assert fact["mem"]["provenance"]["sources"] == [{"kind": "reference", "id": ref_id}]


async def test_a_bad_reference_id_is_refused(project_with_doc):
    """The reference is validated BEFORE any write — an unresolvable source is worse
    than a failed batch (the fact would read as attested while pointing at nothing)."""
    pid, _idx, uid = project_with_doc
    from fastapi import HTTPException
    from memory.apply import apply_memory_verdicts

    run = await _run(pid, uid)
    with pytest.raises(HTTPException) as exc:
        await apply_memory_verdicts(
            project_id=pid, run_id=run.run_id,
            verdicts=[{"action": "new", "title": "Т: ф", "text": "x"}],
            reference_id="not-a-real-reference",
        )
    assert exc.value.status_code == 400


async def test_a_served_fact_names_the_reference_it_came_from(project_with_doc):
    """The journal thread is READABLE at the point of use: a fact returned by retrieval
    carries its source reference (id + title), not only in storage. A consumer that
    cannot see where a fact came from cannot check it against the material."""
    pid, _idx, uid = project_with_doc
    ref_id = await ensure_memory_reference(pid, "provenance-at-retrieval")
    from memory.apply import apply_memory_verdicts

    run = await _run(pid, uid)
    res = await apply_memory_verdicts(
        project_id=pid, run_id=run.run_id, reference_id=ref_id,
        verdicts=[{"action": "new", "title": "Т: ф", "text": "x"}],
    )
    from db import get_db
    from retrieval import RetrievalHit, _load_fact_payloads

    hits = await _load_fact_payloads(await get_db(), [RetrievalHit(
        kind="memory", parent_id=res["created"][0], parent_title="Т: ф",
        heading=None, snippet="x", offset_start=None, offset_end=None, score=1.0,
    )])
    assert [s["id"] for s in hits[0].sources] == [ref_id]
    assert hits[0].sources[0]["title"], "a source without a title is unreadable"


async def test_every_live_fact_resolves_to_a_real_reference(project_with_doc):
    """Recoverability: the journal thread is intact — every live fact's source id is a
    real reference row in its own project. If this ever breaks, "rebuild from the log"
    silently loses facts."""
    pid, _idx, uid = project_with_doc
    await _apply(pid, uid, [
        {"action": "new", "title": "А: 1", "text": "one"},
        {"action": "new", "title": "Б: 2", "text": "two"},
    ], portion="p1")
    from db import get_db

    db = await get_db()
    facts = await db.query(
        "SELECT mem FROM documents WHERE project_id = $pid AND is_memory = true "
        "AND mem_active != false AND deleted_at IS NONE", {"pid": pid},
    )
    ref_ids = {
        s["id"]
        for f in (facts or [])
        for s in ((f.get("mem") or {}).get("provenance") or {}).get("sources") or []
        if s.get("kind") == "reference"
    }
    real = await db.query(
        "SELECT VALUE meta::id(id) FROM documents WHERE project_id = $pid "
        "AND is_reference = true AND deleted_at IS NONE", {"pid": pid},
    )
    real_ids = set(real or [])
    assert ref_ids, "no fact carried a reference source"
    assert ref_ids <= real_ids, f"facts cite non-existent references: {ref_ids - real_ids}"
