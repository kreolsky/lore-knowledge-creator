"""D8 — references are the journal; the knowledge base is rebuildable from them.

Plan `.kilo/plans/1786230000000-memory-flatten-to-facts.md` D8. Every fact is stamped
with the `reference_id` it came from (`mem.provenance.sources`), references are
immutable, and consolidation is re-runnable over them. That is what licenses "drop and
rebuild" as a migration strategy. These tests pin the stamping and the recoverability
property: every live fact points at a real reference, and the reference graph is
intact enough to re-derive the facts.
"""

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
