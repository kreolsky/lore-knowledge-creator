"""Staleness in the consolidation pipeline — named rejections + remedied 404s.

Plan `.kilo/plans/1786753729393-consolidation-pipeline-staleness.md`. The pipeline's
convention is "name it, never ignore it"; the apply path violated it: a stale fact_id
in ONE verdict killed the WHOLE batch with a bare 404, while the module already had
the right mechanism — the per-verdict `rejected` list with 400-only-when-nothing-
valid. These tests pin layer (c) of the unified address-failure contract: the
rejection names WHICH verdict failed, the id that failed, and the one-turn recovery
action (re-read the live index via get_memory_facts).

The SECURITY EXCEPTION lives here too: the scope wall is a boundary, not staleness —
an out-of-scope fact_id stays a whole-call hard failure and still rolls the batch's
creations back, exactly as before.
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


async def _apply(pid, uid, verdicts):
    from memory.apply import apply_memory_verdicts

    run = await _run(pid, uid)
    return await apply_memory_verdicts(
        project_id=pid, run_id=run.run_id, verdicts=verdicts,
        reference_id=await ensure_memory_reference(pid),
    )


async def _create(pid, uid, title="Т: суть", text="тело факта") -> str:
    res = await _apply(pid, uid, [{"action": "new", "title": title, "text": text}])
    return res["created"][0]


async def _mem_active(doc_id) -> bool | None:
    from db import get_db

    db = await get_db()
    rows = await db.query(
        "SELECT mem_active FROM type::record('documents', $id)", {"id": doc_id},
    )
    return (rows[0] or {}).get("mem_active") if rows else None


# ─── per-verdict rejections (layer c) ─────────────────────────────────────────


async def test_a_missing_fact_id_in_a_mixed_batch_is_a_named_rejection(project_with_doc):
    """The measured cohort's largest class: ONE stale id must not kill the valid
    verdicts beside it. The rejection names the id and the remedy; the valid verdict
    applies; the rejected verdict creates nothing (so compensation never sees it)."""
    pid, _idx, uid = project_with_doc
    stale = "no-such-fact"
    res = await _apply(pid, uid, [
        {"action": "new", "title": "Т: новое", "text": "brand new knowledge"},
        {"action": "supersede", "fact_id": stale, "reason": "fix", "text": "z"},
    ])
    assert res["actions"]["new"] == 1  # the valid verdict applied
    assert len(res["rejected"]) == 1
    rej = res["rejected"][0]
    assert rej["index"] == 1
    # Uniform rejection format: EVERY gate's reason carries the verdict index
    # (preflight prefixes them all — staleness, duplicate, validation alike).
    assert rej["reason"].startswith("verdict 1: ")
    assert stale in rej["reason"]
    assert "get_memory_facts" in rej["reason"]  # the one-turn recovery action
    # Rejected verdicts create nothing — `created` carries ONLY the applied one.
    assert len(res["created"]) == 1


async def test_a_retired_fact_id_is_a_named_rejection(project_with_doc):
    """The flagship staleness case: the id is exactly right, the entity is gone —
    the fact was retired by an EARLIER batch in the same run (merged mid-run).
    Every live read channel (index, get_memory_facts) already refuses it; the apply
    path must refuse it too, BY NAME, not write into a zombie fact."""
    pid, _idx, uid = project_with_doc
    a = await _create(pid, uid, title="А: один", text="a body")
    b = await _create(pid, uid, title="А: два", text="b body")
    await _apply(pid, uid, [{"action": "merge", "fact_id": a, "absorb_id": b}])
    retired_id = b if await _mem_active(b) is False else a

    res = await _apply(pid, uid, [
        {"action": "new", "title": "Т: другое", "text": "unrelated knowledge"},
        {"action": "supersede", "fact_id": retired_id, "reason": "fix", "text": "z"},
    ])
    assert res["actions"]["new"] == 1
    assert len(res["rejected"]) == 1
    assert retired_id in res["rejected"][0]["reason"]
    assert "get_memory_facts" in res["rejected"][0]["reason"]


async def test_an_all_stale_batch_is_a_400_naming_the_ids(project_with_doc):
    """Nothing valid → the pinned 400 contract (a 200 with an empty result reads as
    success). The detail names EVERY stale id so the model can correct incrementally."""
    pid, _idx, uid = project_with_doc
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _apply(pid, uid, [
            {"action": "supersede", "fact_id": "stale-one", "reason": "r", "text": "x"},
            {"action": "supersede", "fact_id": "stale-two", "reason": "r", "text": "y"},
        ])
    assert exc.value.status_code == 400
    assert "stale-one" in exc.value.detail
    assert "stale-two" in exc.value.detail
    assert "get_memory_facts" in exc.value.detail


async def test_a_stale_absorb_id_is_a_named_rejection(project_with_doc):
    """`absorb_id` resolves through the same `_require_fact_in_scope` and is the same
    agent-supplied staleness class — the rejection names the FIELD so the model knows
    which address failed."""
    pid, _idx, uid = project_with_doc
    live = await _create(pid, uid, title="А: живой", text="live body")
    res = await _apply(pid, uid, [
        {"action": "merge", "fact_id": live, "absorb_id": "gone-fact"},
        {"action": "new", "title": "Т: другое", "text": "unrelated knowledge"},
    ])
    assert len(res["rejected"]) == 1
    reason = res["rejected"][0]["reason"]
    assert "gone-fact" in reason
    assert "absorb_id" in reason
    assert "get_memory_facts" in reason
    # The live target was NOT touched (the verdict was refused whole).
    assert res["facts"] == [{"id": res["created"][0]}]


async def test_the_under_lock_recheck_refuses_a_fact_retired_mid_run(project_with_doc):
    """The pre-lock staleness gate and the lock-held `_require_fact_in_scope` are not
    atomic: a concurrent apply (other replica — the lock is per-process, see apply's
    LIMITATION) can retire a fact between the two. The under-lock re-assert must
    refuse a retired fact, so a zombie target is never written through even when it
    lost only the race."""
    pid, _idx, uid = project_with_doc
    from fastapi import HTTPException
    from memory._apply_resolution import _require_fact_in_scope

    run = await _run(pid, uid)
    a = await _create(pid, uid, title="А: один", text="a body")
    b = await _create(pid, uid, title="А: два", text="b body")
    await _apply(pid, uid, [{"action": "merge", "fact_id": a, "absorb_id": b}])
    retired_id = b if await _mem_active(b) is False else a

    with pytest.raises(HTTPException) as exc:
        await _require_fact_in_scope(run.scope_root, retired_id, pid)
    assert exc.value.status_code == 404


# ─── the wall is NOT staleness (security exception) ───────────────────────────


async def test_an_out_of_scope_fact_id_stays_a_whole_call_failure(project_with_doc):
    """INVARIANT(security): the wall wins over staleness reporting. A fact that
    RESOLVES but sits outside the run's Memory subtree is a boundary violation — a
    whole-call hard failure (never a per-verdict rejection), and the batch's created
    facts are still compensated away."""
    pid, idx_id, uid = project_with_doc
    from fastapi import HTTPException

    from db import create_record

    out_of_scope = "stale-wall-fact"
    await create_record("documents", out_of_scope, {
        "project_id": pid, "parent_id": idx_id,
        "title": "Т: вне стены", "content": "x", "path": out_of_scope,
        "is_memory": True, "mem_active": True,
        "mem": {"merge_count": 0, "version_history": [],
                "provenance": {"run_id": "seed", "sources": []}},
    })
    with pytest.raises(HTTPException) as exc:
        await _apply(pid, uid, [
            {"action": "new", "title": "Т: новое", "text": "created before the wall"},
            {"action": "merge", "fact_id": out_of_scope, "text": "y"},
        ])
    # 403 = the wall signalled; 404 also acceptable (missing row). Either way the
    # call FAILED — it was not converted into a rejection.
    assert exc.value.status_code in (403, 404)
    # The `new` verdict's fact was compensated (hard-deleted), not left behind.
    res = await _apply(pid, uid, [{"action": "skip", "reason": "probe"}])
    assert res["stats"]["facts"] == 1  # only the out-of-scope seed remains


# ─── remedied 404s (layer c at the call level) ────────────────────────────────


async def test_unknown_run_404_names_the_remedy(project_with_doc):
    """A wrong/expired run_id is a 404 — now carrying the recovery action: runs are
    resumable by construction, so the remedy is consolidate_memory on the target."""
    pid, _idx, uid = project_with_doc
    from fastapi import HTTPException
    from memory.apply import apply_memory_verdicts

    with pytest.raises(HTTPException) as exc:
        await apply_memory_verdicts(
            project_id=pid, run_id="no-such-run",
            verdicts=[{"action": "skip", "reason": "probe"}],
            reference_id="irrelevant-until-the-run-resolves",
        )
    assert exc.value.status_code == 404
    assert "consolidate_memory" in exc.value.detail
    assert "resum" in exc.value.detail.lower()
