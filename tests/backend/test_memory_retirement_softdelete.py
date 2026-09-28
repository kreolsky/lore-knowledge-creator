"""R0a (plan phase-b-debt-payoff, DEC2 variant C) — retirement SOFT-DELETES the fact.

Retirement sets `deleted_at` in the same UPDATE as `mem_active = false`, so the
project-wide `deleted_at IS NONE` clause every reader already carries becomes the ONE
invisibility axis; `mem_active` survives as the label (memory/stats.py +
idx_documents_memory). Two readers filtered `deleted_at` but not `mem_active`
(search_exec's SELECT_CANDS, retrieval's `_allowed_fact_ids`) — the leaks DEC2 was
opened over — and they close by the WRITE, not by a reader patch.

Every test here drives the REAL retirement path (an apply merge-absorb): the contract
under test is the write and the surfaces' shared predicate, not one WHERE clause.
"""

import pytest_asyncio
from memory_ref import ensure_memory_reference

PID = "test-project-001"
TOKEN = "зетарин"


@pytest_asyncio.fixture(autouse=True)
async def _clean_memory_space(test_db):
    await test_db.query(
        "DELETE documents WHERE project_id = $pid AND is_memory = true",
        {"pid": PID},
    )


async def _run(pid: str, uid: str):
    from memory.run_key import mint_memory_run_key

    return await mint_memory_run_key(user_id=uid, project_id=pid)


async def _apply(pid, uid, verdicts, portion=""):
    from memory.apply import apply_memory_verdicts

    run = await _run(pid, uid)
    return await apply_memory_verdicts(
        project_id=pid, run_id=run.run_id, verdicts=verdicts,
        reference_id=await ensure_memory_reference(pid, portion),
    )


async def _fact(doc_id):
    from db import get_db

    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id, title, content, mem_active, deleted_at, mem "
        "FROM type::record('documents', $id)", {"id": doc_id},
    )
    return rows[0]


async def _create(pid, uid, title="Тема: суть", text="тело факта"):
    res = await _apply(pid, uid, [{"action": "new", "title": title, "text": text}])
    return res["created"][0]


async def _retire_one_of_two(pid, uid):
    """Create two TOKEN-bearing facts, merge-fold them; return (live_id, retired_id)."""
    a = await _create(pid, uid, title="А: один", text=f"{TOKEN} кристалл")
    b = await _create(pid, uid, title="А: два", text=f"{TOKEN} осколок")
    await _apply(pid, uid, [{"action": "merge", "fact_id": a, "absorb_id": b}])
    doc_a = await _fact(a)
    live_id, retired_id = (a, b) if doc_a["mem_active"] is True else (b, a)
    return live_id, retired_id


async def test_retirement_soft_deletes_the_fact(project_with_doc):
    """DEC2 variant C: the retired doc carries `deleted_at` — that column, not
    `mem_active`, is what hides it from every reader. Two invisibility axes on one
    table are the defect; this write is the collapse to one."""
    pid, _idx, uid = project_with_doc
    _live_id, retired_id = await _retire_one_of_two(pid, uid)
    retired = await _fact(retired_id)
    assert retired["mem_active"] is False
    assert retired["deleted_at"] is not None, (
        "retirement left deleted_at NONE — a retired fact stays fetchable by every "
        "deleted_at-filtering reader (the exact leak class R0a exists to close)"
    )
    # The label triple is intact (undo payload for a wrong machine-chosen merge).
    assert retired["mem"]["superseded_by"] is not None


async def test_retired_fact_absent_from_allowed_fact_ids(project_with_doc):
    """The retrieval leak (DEC2 evidence #2): `_allowed_fact_ids`' fact scan filters
    `deleted_at` but not `mem_active`, so a retired fact stayed in the subtree-narrow
    fact set. The retirement write closes it with no reader change."""
    pid, idx, uid = project_with_doc

    from db import get_db
    from retrieval import _allowed_fact_ids

    live_id, retired_id = await _retire_one_of_two(pid, uid)
    db = await get_db()
    allowed = await _allowed_fact_ids(db, pid, {idx})
    assert live_id in allowed, "sanity: the live fact is attributable to the host"
    assert retired_id not in allowed, (
        "retired fact leaked into the subtree-narrow fact set"
    )


async def test_retired_fact_absent_from_search_materials(project_with_doc):
    """The search leak (DEC2 evidence #1): SELECT_CANDS filters `deleted_at` but not
    `mem_active`, so a retired fact surfaced in search_materials' lexical layer.
    Drives the real two-layer search; both facts' wording matches the query, so a
    leak shows up as the retired id among the hits."""
    pid, _idx, uid = project_with_doc

    from agent.search_exec import _search_materials_exec

    live_id, retired_id = await _retire_one_of_two(pid, uid)
    res = await _search_materials_exec(
        project_id=pid, user={"user_id": uid}, query=TOKEN, k=5,
    )
    hit_ids = {h.get("doc_id") or h.get("parent_id") for h in res["hits"]}
    assert live_id in hit_ids, f"sanity: the live fact matches the query — {res}"
    assert retired_id not in hit_ids, (
        f"retired fact leaked into search_materials hits — {res['hits']}"
    )


async def test_retired_fact_absent_from_memory_serve_surfaces(project_with_doc):
    """Index, on-demand bodies, and merge candidates all refuse the retired fact —
    post-R0a by their shared `deleted_at IS NONE` predicate."""
    pid, _idx, uid = project_with_doc

    from memory._candidates import _attach_candidate_bodies
    from memory._serve import _load_memory_index, get_memory_facts

    from db import get_db

    live_id, retired_id = await _retire_one_of_two(pid, uid)
    db = await get_db()

    index_ids = {f["id"] for f in await _load_memory_index(db, pid)}
    assert live_id in index_ids
    assert retired_id not in index_ids

    served = await get_memory_facts(project_id=pid, ids=[live_id, retired_id])
    assert {f["id"] for f in served["facts"]} == {live_id}
    assert retired_id in served["missing"]

    cands = await _attach_candidate_bodies(db, pid, [
        {"id": retired_id, "title": "r", "score": 1.0},
    ])
    assert cands[0]["text"] == "", "a retired fact must not hand its body to a candidate"


async def test_memory_stats_still_counts_retired_under_the_label(project_with_doc):
    """`mem_active` as LABEL: the stats scan must still SEE soft-deleted retired
    facts — it cannot filter `deleted_at` like every reader does, or `retired`
    silently drops to zero the moment R0a lands."""
    pid, _idx, uid = project_with_doc

    from memory.stats import memory_stats

    _live_id, retired_id = await _retire_one_of_two(pid, uid)
    assert await _fact(retired_id)

    s = await memory_stats(pid)
    assert s["retired"] == 1, f"retired count lost the soft-deleted fact: {s}"
    assert s["facts"] == 1, s


async def test_user_deleted_fact_counts_nowhere_in_stats(project_with_doc):
    """The label distinguishes retirement from a USER deletion: a fact the user
    soft-deletes (mem_active still true) is counted neither live nor retired."""
    pid, _idx, uid = project_with_doc

    from memory.stats import memory_stats

    from db import get_db

    live_id, _retired_id = await _retire_one_of_two(pid, uid)
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": live_id},
    )
    assert await memory_stats(pid) == {"facts": 0, "retired": 1}
