"""Project memory — fact-level apply mechanics (Order 2).

Plan `.kilo/plans/1786230000000-memory-flatten-to-facts.md`. A FACT is a document
whose `content` IS the fact. These tests guard the irreducible shapes: `new` creates a
fact-doc, `merge` absorbs material (and folds two facts), `supersede` rewrites a fact
in place (the doc id stays, the old wording becomes history), `skip` does nothing.
"""

import logging

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


# ─── new ───────────────────────────────────────────────────────────────────────


async def test_new_creates_a_live_fact_document(project_with_doc):
    pid, _idx, uid = project_with_doc
    res = await _create_one(pid, uid)
    assert res["actions"]["new"] == 1
    assert len(res["created"]) == 1
    fact = await _fact(res["created"][0])
    assert fact["is_memory"] is True
    assert fact["mem_active"] is True
    assert fact["content"] == "снижает риск"
    assert fact["title"] == "Аспирин: антиагрегант"
    mem = fact["mem"]
    assert mem["merge_count"] == 0
    assert mem["version_history"] == []
    # Provenance carries the reference (D8).
    assert mem["provenance"]["sources"][0]["kind"] == "reference"


async def test_new_titles_its_fact_with_the_subject_prefix(project_with_doc):
    """D3: a fact's title is subject-led (`Субъект: суть`). The server never parses
    the prefix — it stores the title verbatim and a prefix-less title is a normal
    searchable fact (degrades gracefully)."""
    pid, _idx, uid = project_with_doc
    res = await _apply(pid, uid, [{"action": "new", "title": "bare title no prefix", "text": "x"}])
    fact = await _fact(res["created"][0])
    assert fact["title"] == "bare title no prefix"


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


# ─── merge (absorb) ────────────────────────────────────────────────────────────


async def test_merge_folds_two_facts_and_retires_the_absorbed(project_with_doc):
    """A merge with `absorb_id` folds two facts: one survives, the other retires
    pointing at it. The survivor's id is chosen structurally (established hub → most
    recently verified → oldest), not by which the agent named first."""
    pid, _idx, uid = project_with_doc
    a = await _create_one(pid, uid, title="Тромбоциты: клетки", text="участвуют в свёртывании")
    b = await _create_one(pid, uid, title="Тромбоциты: роль", text="формируют пробку")
    await _apply(
        pid, uid,
        [{"action": "merge", "fact_id": a["created"][0], "absorb_id": b["created"][0]}],
    )
    live = await _fact(a["created"][0])
    other = await _fact(b["created"][0])
    # Exactly one is live, one retired — order decided structurally.
    live_doc, retired_doc = (
        (live, other) if live["mem_active"] is True else (other, live)
    )
    assert live_doc["mem_active"] is True
    assert retired_doc["mem_active"] is False
    assert retired_doc["mem"]["superseded_by"] == live_doc["id"]
    assert live_doc["mem"]["merge_count"] == 1


# ─── supersede ─────────────────────────────────────────────────────────────────


async def test_supersede_rewrites_a_fact_in_place_and_keeps_history(project_with_doc):
    """A supersede rewrites a fact's body IN PLACE — the doc id is stable (a link
    target survives), and the old wording moves onto version_history."""
    pid, _idx, uid = project_with_doc
    created = await _create_one(pid, uid, text="старая формулировка")
    fact_id = created["created"][0]
    res = await _apply(
        pid, uid,
        [{"action": "supersede", "fact_id": fact_id, "reason": "уточнено",
          "text": "новая формулировка"}],
    )
    assert res["actions"]["supersede"] == 1
    # The SAME doc id is touched (in place) — no new fact created.
    assert res["created"] == []
    assert res["touched"] == [fact_id]
    fact = await _fact(fact_id)
    assert fact["content"] == "новая формулировка"
    assert fact["mem_active"] is True  # still live — a correction, not a retirement
    history = fact["mem"]["version_history"]
    assert len(history) == 1
    assert history[0]["text"] == "старая формулировка"
    assert history[0]["supersede_reason"] == "уточнено"


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


# ─── compensation ──────────────────────────────────────────────────────────────


async def test_a_failed_batch_leaves_no_created_fact_behind(project_with_doc):
    """A verdict batch either applies, or leaves NONE of its created facts behind.
    A `new` followed by a verdict that hard-fails mid-persistence must hard-delete
    the new fact on compensation. The trigger is the scope WALL (a memory fact
    outside the Memory folder), which stays a whole-call failure under the staleness
    contract — a stale fact_id no longer hard-fails (it is a pre-lock rejection,
    pinned in test_memory_staleness.py)."""
    pid, idx_id, uid = project_with_doc
    from fastapi import HTTPException

    from db import create_record

    # A memory fact-doc parented OUTSIDE the Memory folder (under the index) — the
    # wall rejects it AFTER the `new` already created a fact.
    out_of_scope = "cmp-fact-1"
    await create_record("documents", out_of_scope, {
        "project_id": pid, "parent_id": idx_id,
        "title": "Т: вне стены", "content": "x", "path": out_of_scope,
        "is_memory": True, "mem_active": True,
        "mem": {"merge_count": 0, "version_history": [],
                "provenance": {"run_id": "seed", "sources": []}},
    })
    with pytest.raises(HTTPException):
        await _apply(pid, uid, [
            {"action": "new", "title": "t", "text": "x"},
            {"action": "merge", "fact_id": out_of_scope, "text": "y"},
        ])
    # No live fact survives the failed batch except the out-of-scope seed itself.
    res = await _apply(pid, uid, [{"action": "skip", "reason": "probe"}])
    assert res["stats"]["facts"] == 1


# ─── inline fact embed (the vector rides the apply) ───────────────────────────


async def test_new_fact_has_its_chunk_before_the_response(project_with_doc):
    """A `new` verdict leaves a doc_chunks row before apply returns.

    The vector used to arrive one EMBEDDING_COOLDOWN_SEC later through the
    keystroke debounce, so the next portion's dedup gate and merge candidates
    could not see the fact this batch just wrote.
    """
    pid, _idx, uid = project_with_doc
    from unittest.mock import AsyncMock, patch

    async def fake_embed_texts(texts, *, instruction=None):
        return [[0.1, 0.2] for _ in texts]

    with patch("embeddings._ensure_config", new_callable=AsyncMock), \
         patch("embeddings.embed_texts", side_effect=fake_embed_texts):
        res = await _create_one(pid, uid)
    fact_id = res["created"][0]
    from db import get_db

    db = await get_db()
    rows = await db.query(
        "SELECT count() AS c FROM doc_chunks WHERE document_id = $id GROUP ALL",
        {"id": fact_id},
    )
    assert rows and rows[0]["c"] >= 1, "fact left the apply with no vector"


async def test_embed_failure_never_fails_the_apply(project_with_doc, caplog):
    """A provider failure in the inline embed costs the early vector, never the
    apply: the result returns intact and a WARNING names the fact for the
    operator — the debounced job is the catch-up path."""
    pid, _idx, uid = project_with_doc
    from unittest.mock import AsyncMock, patch

    import httpx

    with patch("embeddings._ensure_config", new_callable=AsyncMock), \
         patch("embeddings.embed_texts", side_effect=httpx.ConnectError("refused")), \
         caplog.at_level(logging.WARNING, logger="embeddings"):
        res = await _create_one(pid, uid)

    assert res["actions"]["new"] == 1
    assert len(res["created"]) == 1
    assert any("Inline reembed failed" in r.message for r in caplog.records)


async def test_inline_embed_runs_with_the_lock_released(project_with_doc):
    """The inline embed runs AFTER the lock block — the lock key is absent in
    Redis when reembed_now is called.

    The lock is a 30 s TTL with no heartbeat and the embed round-trip can run
    60 s: embedding under the lock would let it lapse mid-embed and interleave
    two applies."""
    pid, _idx, uid = project_with_doc
    from unittest.mock import patch

    from redis_pool import get_redis

    seen = {}

    async def spy_reembed_now(entity_id, project_id):
        r = await get_redis()
        seen["lock_present"] = await r.get(f"editlock:memory:{project_id}")

    with patch("embeddings.reembed_now", side_effect=spy_reembed_now):
        res = await _create_one(pid, uid)

    assert len(res["created"]) == 1, "the locked body ran (the fact was created)"
    assert "lock_present" in seen, "reembed_now was never called over `written`"
    assert seen["lock_present"] is None, "embed ran while the lock was still held"
