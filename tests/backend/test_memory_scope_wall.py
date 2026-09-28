"""INVARIANT(security) — the scope wall survives the collapse unchanged.

Plan `.kilo/plans/1786230000000-memory-flatten-to-facts.md`. A consolidation run writes
ONLY through a key scoped to the project's `Memory` folder, and that wall is the
EXISTING one (SYSTEM: scope). A `fact_id` naming a document outside that subtree is
refused by the apply path (`_require_fact_in_scope`), never touched.
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


async def _apply(pid, uid, verdicts, ref_id=None):
    from memory.apply import apply_memory_verdicts

    run = await _run(pid, uid)
    return await apply_memory_verdicts(
        project_id=pid, run_id=run.run_id, verdicts=verdicts,
        reference_id=ref_id or await ensure_memory_reference(pid),
    )


async def test_a_fact_id_outside_the_memory_folder_is_refused(project_with_doc):
    """A merge/supersede targeting a memory fact OUTSIDE the run's Memory subtree is
    refused — the wall wins over a stale or hostile id."""
    pid, idx_id, uid = project_with_doc
    from fastapi import HTTPException

    from db import create_record

    # A memory fact-doc parented OUTSIDE the Memory folder (directly under the index).
    out_of_scope = "oom-fact-1"
    await create_record("documents", out_of_scope, {
        "project_id": pid, "parent_id": idx_id,
        "title": "Т: вне стены", "content": "x", "path": out_of_scope,
        "is_memory": True, "mem_active": True,
        "mem": {"merge_count": 0, "version_history": [],
                "provenance": {"run_id": "seed", "sources": []}},
    })
    with pytest.raises(HTTPException) as exc:
        await _apply(pid, uid, [
            {"action": "merge", "fact_id": out_of_scope, "text": "y"},
        ])
    # 403 = the wall signalled (distinct from a 404 "not found"); 404 is also
    # acceptable (an is_memory check or a missing row). Either way, it did NOT apply.
    assert exc.value.status_code in (403, 404)


async def test_a_non_memory_doc_id_is_refused_as_a_fact_target(project_with_doc):
    """A fact_id naming an ordinary document (is_memory != true) is refused — only
    facts are valid targets, and the gate says so uniformly. Staleness contract
    (plan 1786753729393): this is a per-verdict REJECTION naming the id + remedy, no
    longer a whole-call 404 (which killed the valid verdicts beside it). Nothing
    valid in this batch → the pinned 400."""
    pid, idx_id, uid = project_with_doc
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _apply(pid, uid, [
            {"action": "merge", "fact_id": idx_id, "text": "y"},
        ])
    assert exc.value.status_code == 400
    assert idx_id in exc.value.detail
    assert "get_memory_facts" in exc.value.detail


async def test_a_cross_project_fact_id_is_refused(project_with_doc):
    """A fact_id from another project is refused uniformly (no existence oracle) —
    as a per-verdict rejection whose reason does not distinguish missing from
    cross-project (same info as the old whole-call 404, now named)."""
    pid, _idx, uid = project_with_doc
    from fastapi import HTTPException

    from db import create_record

    # A memory fact in a DIFFERENT project.
    other_pid = "test-project-oom"
    await create_record("projects", other_pid, {"name": "other", "status": "active"})
    other_fact = "oom-fact-cross"
    await create_record("documents", other_fact, {
        "project_id": other_pid, "parent_id": None,
        "title": "Т: чужой", "content": "x", "path": other_fact,
        "is_memory": True, "mem_active": True,
        "mem": {"merge_count": 0, "version_history": [],
                "provenance": {"run_id": "seed", "sources": []}},
    })
    try:
        with pytest.raises(HTTPException) as exc:
            await _apply(pid, uid, [
                {"action": "merge", "fact_id": other_fact, "text": "y"},
            ])
        assert exc.value.status_code == 400
        assert other_fact in exc.value.detail
        assert "get_memory_facts" in exc.value.detail
    finally:
        from db import get_db

        db = await get_db()
        await db.query("DELETE type::record('documents', $id)", {"id": other_fact})
        await db.query("DELETE type::record('projects', $id)", {"id": other_pid})
