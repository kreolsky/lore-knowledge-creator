"""D2 — retirement keeps the document and points it at its successor; never deletes.

Plan `.kilo/plans/1786230000000-memory-flatten-to-facts.md` D2. The split existed so a
fact would be a stable link target — `[text](doc:id)` must not orphan when knowledge
moves. Under the fact model:
  - a MERGE-ABSORB retires the absorbed fact (SOFT-DELETE: `deleted_at` + `mem_active
    = false` + `superseded_by` → survivor, vector dropped — R0a / DEC2 variant C) but
    KEEPS the row, so a link still resolves;
  - a SUPERSEDE rewrites a fact IN PLACE (the doc id stays — its own stable target).

These tests pin both, plus that a retired fact leaves the index and its vector.
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
        "SELECT meta::id(id) AS id, title, content, mem_active, mem "
        "FROM type::record('documents', $id)", {"id": doc_id},
    )
    return rows[0]


async def _has_vector(doc_id) -> bool:
    from db import get_db

    db = await get_db()
    rows = await db.query(
        "SELECT count() AS n FROM doc_chunks WHERE document_id = $id GROUP ALL",
        {"id": doc_id},
    )
    return bool(rows and int(rows[0]["n"]) > 0)


async def _create(pid, uid, title="Тема: суть", text="тело факта"):
    res = await _apply(pid, uid, [{"action": "new", "title": title, "text": text}])
    return res["created"][0]


async def test_merge_absorb_keeps_the_document_and_points_at_the_survivor(project_with_doc):
    pid, _idx, uid = project_with_doc
    a = await _create(pid, uid, title="А: один", text="a body")
    b = await _create(pid, uid, title="А: два", text="b body")
    await _apply(pid, uid, [{"action": "merge", "fact_id": a, "absorb_id": b}])

    doc_a = await _fact(a)
    doc_b = await _fact(b)
    live, retired = (doc_a, doc_b) if doc_a["mem_active"] is True else (doc_b, doc_a)
    # The retired document SURVIVES (never deleted) — content + title intact.
    assert retired["content"] in ("a body", "b body")
    assert retired["mem_active"] is False
    assert retired["mem"]["superseded_by"] == live["id"]
    assert retired["mem"]["supersede_reason"] is not None


async def test_merge_absorb_drops_the_vector_but_keeps_the_doc(project_with_doc):
    """Retirement is one op (abot2 `retire`): drop the retrieval slot, keep the row."""
    pid, _idx, uid = project_with_doc
    a = await _create(pid, uid, title="А: один", text="a body")
    b = await _create(pid, uid, title="А: два", text="b body")
    await _apply(pid, uid, [{"action": "merge", "fact_id": a, "absorb_id": b}])
    doc_a = await _fact(a)
    retired_id = a if doc_a["mem_active"] is False else b
    # A retired fact NEVER carries a vector, whether or not it had one before.
    assert await _has_vector(retired_id) is False
    # …and the doc is still there.
    assert (await _fact(retired_id))["content"] in ("a body", "b body")


async def test_a_retired_fact_leaves_the_index_but_resolves_by_id(project_with_doc):
    """A retired fact is out of the served index (soft-delete filters it), yet the
    document row still resolves — a link to it is never orphaned."""
    pid, _idx, uid = project_with_doc
    from memory._serve import _load_memory_index

    from db import get_db

    a = await _create(pid, uid, title="А: один", text="a")
    b = await _create(pid, uid, title="А: два", text="b")
    await _apply(pid, uid, [{"action": "merge", "fact_id": a, "absorb_id": b}])
    doc_a = await _fact(a)
    live_id = a if doc_a["mem_active"] is True else b
    retired_id = b if live_id == a else a

    db = await get_db()
    index = await _load_memory_index(db, pid)
    index_ids = {f["id"] for f in index}
    assert live_id in index_ids
    assert retired_id not in index_ids  # retired → out of the index
    # But the retired doc row still exists (link target survives) — soft-deleted,
    # which is exactly what keeps it out of every reader while it stays restorable.
    rows = await db.query(
        "SELECT meta::id(id) AS id, deleted_at FROM type::record('documents', $id)",
        {"id": retired_id},
    )
    assert rows and rows[0]["id"] == retired_id
    assert rows[0]["deleted_at"] is not None


async def test_supersede_keeps_the_doc_id_stable(project_with_doc):
    """A correction rewrites the fact in place — the doc id never changes, so every
    `[text](doc:id)` link made before the correction still lands on the live fact."""
    pid, _idx, uid = project_with_doc
    fact_id = await _create(pid, uid, text="was wrong")
    await _apply(pid, uid, [{"action": "supersede", "fact_id": fact_id, "reason": "fix", "text": "now right"}])
    fact = await _fact(fact_id)
    assert fact["content"] == "now right"
    assert fact["mem_active"] is True
    # The old wording is history on the SAME doc.
    assert fact["mem"]["version_history"][0]["text"] == "was wrong"


async def test_history_channel_serves_the_retired_wording(project_with_doc):
    """The on-request `get_fact_history` returns the retired wording + reason — the
    ONLY channel through which a superseded fact's old text reaches the agent."""
    pid, _idx, uid = project_with_doc
    from memory.task_builder import get_fact_history

    fact_id = await _create(pid, uid, text="первая редакция")
    await _apply(pid, uid, [{"action": "supersede", "fact_id": fact_id, "reason": "уточнено", "text": "вторая редакция"}])
    history = await get_fact_history(project_id=pid, fact_id=fact_id)
    assert history["text"] == "вторая редакция"
    assert history["revisions"] == 1
    assert history["history"][0]["text"] == "первая редакция"
    assert history["history"][0]["supersede_reason"] == "уточнено"


async def test_history_404_for_a_retired_id_names_the_remedy(project_with_doc):
    """Staleness contract (plan 1786753729393): the retired-id 404 must carry the
    one-turn recovery action — fetch the live successor, then ask for its history
    (a bare 404 left the agent re-asking, the loop this channel exists to end)."""
    pid, _idx, uid = project_with_doc
    from fastapi import HTTPException
    from memory.task_builder import get_fact_history

    a = await _create(pid, uid, title="А: один", text="a")
    b = await _create(pid, uid, title="А: два", text="b")
    await _apply(pid, uid, [{"action": "merge", "fact_id": a, "absorb_id": b}])
    retired_id = b if (await _fact(b))["mem_active"] is False else a
    with pytest.raises(HTTPException) as exc:
        await get_fact_history(project_id=pid, fact_id=retired_id)
    assert exc.value.status_code == 404
    assert "get_memory_facts" in exc.value.detail


async def test_history_404_for_an_unknown_id_is_uniform_with_the_remedy(project_with_doc):
    """An unknown id gets the SAME 404 text as a retired one — no existence oracle,
    and the remedy is stated for both (same information, now with a next action)."""
    pid, _idx, _uid = project_with_doc
    from fastapi import HTTPException
    from memory.task_builder import get_fact_history

    with pytest.raises(HTTPException) as exc:
        await get_fact_history(project_id=pid, fact_id="no-such-fact")
    assert exc.value.status_code == 404
    assert "get_memory_facts" in exc.value.detail
