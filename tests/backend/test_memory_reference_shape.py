"""D5 — a run must report its own shape: facts-per-reference + batches-per-reference.

The extraction quota surfaced only because someone read the database by hand: gemini/pro
emitted exactly 5 verdicts per reference, one apply call each, and nothing in the run's
own output named that shape. facts-per-reference is recoverable from provenance (the one
thread from a fact back to its source material); batches-per-reference is not, so it is
counted per run, ephemerally, and both are surfaced in the apply result next to `stats`
and in the closing portion. Sourced from what the server did, not what the agent recalls.
"""

import asyncio

import pytest
import pytest_asyncio
from memory_ref import ensure_memory_reference

PID = "test-project-001"


@pytest_asyncio.fixture(autouse=True)
async def _clean_memory_space(project_with_doc, test_db):
    """Hermetic: the loop test drives `_next_portion`, which would otherwise see stale
    unstamped references left by other tests. Wipe references + memory facts + chunks."""
    pid = project_with_doc[0]
    await test_db.query(
        "DELETE documents WHERE project_id = $pid AND is_memory = true", {"pid": pid},
    )
    await test_db.query("DELETE doc_chunks WHERE project_id = $pid", {"pid": pid})
    await test_db.query(
        "DELETE documents WHERE project_id = $pid AND is_reference = true", {"pid": pid},
    )


async def _host_id(pid: str) -> str:
    from db import get_db

    db = await get_db()
    rows = await db.query(
        "SELECT index_doc_id FROM type::record('projects', $pid)", {"pid": pid},
    )
    return rows[0]["index_doc_id"]


async def _run(pid: str, uid: str):
    from memory.run_key import mint_memory_run_key

    return await mint_memory_run_key(user_id=uid, project_id=pid)


async def _apply(pid, run_id, verdicts, ref_id):
    from memory.apply import apply_memory_verdicts

    return await apply_memory_verdicts(
        project_id=pid, run_id=run_id, verdicts=verdicts, reference_id=ref_id,
    )


async def test_facts_by_reference_counts_live_facts_per_reference(project_with_doc):
    """facts-per-reference is grouped from provenance: 2 facts citing A, 1 citing B."""
    pid, _idx, uid = project_with_doc
    ref_a = await ensure_memory_reference(pid, "shape-a")
    ref_b = await ensure_memory_reference(pid, "shape-b")
    run = await _run(pid, uid)
    await _apply(pid, run.run_id, [
        # Two facts that the duplicate gate must NOT fold: the pair only differing by an
        # ordinal ("первый"/"второй … факт") scores 0.86 under giga/480m and is refused at
        # the calibrated 0.82 — the fixture must be two distinct assertions, not a template.
        {"action": "new", "title": "Т: a1", "text": "лекция в материале a длилась девяносто минут"},
        {"action": "new", "title": "Т: a2", "text": "лектор материала a рекомендовал учебник Смирнова"},
    ], ref_a)
    await _apply(pid, run.run_id, [
        {"action": "new", "title": "Т: b1", "text": "единственный факт из материала b"},
    ], ref_b)
    from memory.stats import facts_by_reference

    counts = await facts_by_reference(pid)
    assert counts.get(ref_a) == 2
    assert counts.get(ref_b) == 1


async def test_apply_result_carries_reference_shape(project_with_doc):
    """The apply result gains `reference_shape` next to `stats`: the reference just
    consolidated, its live fact count, and how many apply batches have targeted it."""
    pid, _idx, uid = project_with_doc
    ref = await ensure_memory_reference(pid, "rs-apply")
    run = await _run(pid, uid)
    res = await _apply(pid, run.run_id, [
        {"action": "new", "title": "Т: x", "text": "некоторый факт про предмет X"},
    ], ref)
    shape = res["reference_shape"]
    assert shape["reference_id"] == ref
    assert shape["facts"] == 1
    assert shape["batches"] == 1


async def test_batches_per_reference_increments_across_applies(project_with_doc):
    """Two apply calls for the SAME reference under the SAME run → batches == 2 and
    facts == 2. This is the number D1 is meant to move (one apply each → many)."""
    pid, _idx, uid = project_with_doc
    ref = await ensure_memory_reference(pid, "rs-batches")
    run = await _run(pid, uid)
    await _apply(pid, run.run_id, [
        {"action": "new", "title": "Т: 1", "text": "первый самостоятельный факт"},
    ], ref)
    res2 = await _apply(pid, run.run_id, [
        {"action": "new", "title": "Т: 2", "text": "второй самостоятельный факт иной сути"},
    ], ref)
    assert res2["reference_shape"]["batches"] == 2
    assert res2["reference_shape"]["facts"] == 2


async def test_apply_fails_closed_when_the_store_is_down(
    project_with_doc, monkeypatch,
):
    """Redis unreachable → the memory apply RAISES, it does not proceed unlocked.

    Contract changed with the Redis edit lock (plan replica-independent-guarantees
    step 3): the apply's serialization now lives in Redis, so a store outage fails
    the apply loudly (consistent with the service-wide "Redis down = service down"
    stance). The previous contract — apply anyway, degrade `batches` to null —
    proceeded WITHOUT the lock, the exact cross-replica interleaving the lock
    exists to close."""
    pid, _idx, uid = project_with_doc
    ref = await ensure_memory_reference(pid, "rs-down")
    run = await _run(pid, uid)
    import redis_pool

    async def _boom_get_redis(*_a, **_k):
        raise RuntimeError("store down")

    monkeypatch.setattr(redis_pool, "get_redis", _boom_get_redis)
    with pytest.raises(RuntimeError, match="store down"):
        await _apply(pid, run.run_id, [
            {"action": "new", "title": "Т: d", "text": "факт при недоступном хранилище"},
        ], ref)


async def test_batches_counter_degrades_to_none_when_the_store_is_down(monkeypatch):
    """Unit pin for the counter's OWN fail-open: incr_reference_batches catches a
    store outage and returns None — the report degrades, nothing raises. Reachable
    when Redis drops AFTER the lock was acquired (mid-apply); the apply-level
    from-the-start outage is pinned by the fails-closed test above."""
    import redis_pool
    from memory.run_key import incr_reference_batches

    async def _boom_get_redis(*_a, **_k):
        raise RuntimeError("store down")

    monkeypatch.setattr(redis_pool, "get_redis", _boom_get_redis)
    assert await incr_reference_batches("run-1", "ref-1") is None


async def test_closing_portion_reports_reference_shape(project_with_doc):
    """The closing portion (`complete: true`) carries `reference_shape` for every
    reference under the host — the shape the closing report is composed from."""
    pid, _idx, uid = project_with_doc
    ref_a = await ensure_memory_reference(pid, "close-a")
    await asyncio.sleep(0.05)  # distinct created_at ⇒ deterministic oldest-first order
    ref_b = await ensure_memory_reference(pid, "close-b")
    host = await _host_id(pid)
    run = await _run(pid, uid)
    from memory.task_builder import build_consolidation_task, next_reference

    p1 = await build_consolidation_task(
        project_id=pid, target_doc_id=host, run_id=run.run_id,
    )
    assert p1["reference"]["id"] == ref_a
    await _apply(pid, run.run_id, [
        {"action": "new", "title": "Т: a", "text": "факт извлечённый из материала a"},
    ], ref_a)
    p2 = await next_reference(
        project_id=pid, run_id=run.run_id, completed_reference_id=ref_a,
    )
    assert p2["reference"]["id"] == ref_b
    await _apply(pid, run.run_id, [
        {"action": "new", "title": "Т: b1", "text": "первый факт из материала b"},
        {"action": "new", "title": "Т: b2", "text": "второй факт из материала b иного содержания"},
    ], ref_b)
    closing = await next_reference(
        project_id=pid, run_id=run.run_id, completed_reference_id=ref_b,
    )
    assert closing["complete"] is True
    by_ref = {r["reference_id"]: r for r in closing["reference_shape"]}
    assert set(by_ref) == {ref_a, ref_b}
    assert by_ref[ref_a]["facts"] == 1
    assert by_ref[ref_b]["facts"] == 2
    assert by_ref[ref_a]["batches"] == 1
    assert by_ref[ref_b]["batches"] == 1
