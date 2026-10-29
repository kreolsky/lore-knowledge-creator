"""Project memory — fact-level apply mechanics (Order 2).

Plan `.kilo/plans/1786230000000-memory-flatten-to-facts.md`. A FACT is a document
whose `content` IS the fact. These tests guard the irreducible shapes: `new` creates a
fact-doc, `merge` absorbs material (and folds two facts), `supersede` rewrites a fact
in place (the doc id stays, the old wording becomes history), `skip` does nothing.
"""


import pytest
import pytest_asyncio
from memory_ref import ensure_memory_reference

PID = "test-project-001"


@pytest_asyncio.fixture(autouse=True)
async def _clean_memory_space(test_db):
    """Wipe the project's memory space before each test (one shared project id)."""
    await test_db.query(
        "DELETE documents WHERE project_id = $pid AND is_memory = true",
        {"pid": PID},
    )


async def _run(pid: str, uid: str):
    from memory.run_key import mint_memory_run_key

    return await mint_memory_run_key(user_id=uid, project_id=pid)


async def _apply(
    pid: str, uid: str, verdicts: list[dict], run=None, portion: str = "",
) -> dict:
    from memory.apply import apply_memory_verdicts

    run = run or await _run(pid, uid)
    return await apply_memory_verdicts(
        project_id=pid, run_id=run.run_id, verdicts=verdicts,
        reference_id=await ensure_memory_reference(pid, portion),
    )


async def _fact(doc_id: str) -> dict:
    from db import get_db

    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id, title, content, is_memory, mem_active, mem "
        "FROM type::record('documents', $id)", {"id": doc_id},
    )
    return rows[0]


async def _create_one(pid, uid, title="Аспирин: антиагрегант", text="снижает риск") -> dict:
    """Apply one `new` verdict and return the apply result."""
    return await _apply(pid, uid, [{"action": "new", "title": title, "text": text}])


# ─── skip ──────────────────────────────────────────────────────────────────────


async def test_skip_creates_nothing(project_with_doc):
    pid, _idx, uid = project_with_doc
    res = await _apply(pid, uid, [{"action": "skip", "reason": "эфемерно"}])
    assert res["actions"]["skip"] == 1
    assert res["created"] == []
    assert res["facts"] == []
    assert res["skipped"] == [{"text": None, "reason": "эфемерно"}]


# ─── merge (no absorb) ─────────────────────────────────────────────────────────


async def test_merge_absorbs_a_second_source_into_a_fact(project_with_doc):
    """A merge without `absorb_id` records a second source corroborating a stored
    fact: `merge_count` advances, the new source is folded into provenance, and the
    wording may be updated."""
    pid, _idx, uid = project_with_doc
    created = await _create_one(pid, uid)
    fact_id = created["created"][0]
    res = await _apply(
        pid, uid,
        [{"action": "merge", "fact_id": fact_id, "text": "снижает риск тромбоза"}],
        portion="second",
    )
    assert res["actions"]["merge"] == 1
    fact = await _fact(fact_id)
    assert fact["mem"]["merge_count"] == 1
    assert fact["content"] == "снижает риск тромбоза"
    sources = fact["mem"]["provenance"]["sources"]
    assert len(sources) == 2  # the original + the second portion


# ─── supersede ─────────────────────────────────────────────────────────────────


async def test_supersede_marker_is_served_only_when_there_is_history(project_with_doc):
    """The served marker (`revisions`) is OMITTED at N=0, present after a supersede."""
    pid, _idx, uid = project_with_doc
    from memory._serve import get_memory_facts

    created = await _create_one(pid, uid, text="v0")
    fact_id = created["created"][0]
    served = await get_memory_facts(project_id=pid, ids=[fact_id])
    assert "revisions" not in served["facts"][0]
    await _apply(
        pid, uid,
        [{"action": "supersede", "fact_id": fact_id, "reason": "r", "text": "v1"}],
    )
    served = await get_memory_facts(project_id=pid, ids=[fact_id])
    assert served["facts"][0]["revisions"] == 1


async def test_supersede_target_is_a_field_never_read_from_prose(project_with_doc):
    """The supersede/merge target is the `fact_id` FIELD, never an id scraped from
    fact text. A verdict with no fact_id is rejected, even if the text mentions an id."""
    pid, _idx, uid = project_with_doc
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await _apply(
            pid, uid,
            [{"action": "supersede", "reason": "r", "text": "mentions doc:abc but no fact_id"}],
        )


async def test_merge_absorbing_itself_is_a_named_rejection(project_with_doc):
    """`absorb_id == fact_id` is a pure validation mistake (a fact cannot fold into
    itself; the correction is supersede) — so it is a per-verdict REJECTION decided in
    the pure preflight, never a mid-lock 400 that kills the valid verdicts beside it
    and triggers compensation."""
    pid, _idx, uid = project_with_doc
    target = (await _create_one(pid, uid, title="А: цель", text="body"))["created"][0]
    res = await _apply(pid, uid, [
        {"action": "new", "title": "Т: другое", "text": "unrelated knowledge"},
        {"action": "merge", "fact_id": target, "absorb_id": target, "text": "y"},
    ])
    assert res["actions"]["new"] == 1  # the valid verdict applied and STAYS
    assert len(res["created"]) == 1
    assert len(res["rejected"]) == 1
    rej = res["rejected"][0]
    assert rej["index"] == 1
    assert "absorb_id must differ from fact_id" in rej["reason"]
    assert "supersede" in rej["reason"]  # the correction, not just the violation
