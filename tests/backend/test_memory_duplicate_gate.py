"""D7 — the duplicate gate survives, at fact level.

Plan `.kilo/plans/1786230000000-memory-flatten-to-facts.md` D7. The duplicate decision
is the SERVER's, not the agent's: measured on a re-served portion, the agent chose
`new` over `merge` 8 times out of 9 — three against twins served to it in that same
portion. Supplying the material is necessary and not sufficient. Order 4 reshapes the
gate to fact-level nearest neighbours; Order 2 keeps it functional and pointed at fact
bodies.

The stub embedder keys off markers: two texts sharing a marker are identical vectors
(cosine 1.0), different markers are orthogonal (0.0). Deterministic, so the tests bind
the GATE's behaviour rather than an embedding model's opinion.
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from agent_config import ensure_agent_system_docs
from memory_ref import ensure_memory_reference

from db import create_record, get_db

_SAME = "ОДНОТОЖДЕ"
_OTHER = "ДРУГОЕ"


def _stub_vec(text: str) -> list[float]:
    if _SAME in text:
        return [1.0, 0.0, 0.0, 0.0]
    if _OTHER in text:
        return [0.0, 1.0, 0.0, 0.0]
    return [0.0, 0.0, 1.0, 0.0]


PID = "test-project-001"


@pytest_asyncio.fixture(autouse=True)
async def _clean_memory_space(test_db):
    await test_db.query(
        "DELETE documents WHERE project_id = $pid AND is_memory = true",
        {"pid": PID},
    )
    await test_db.query("DELETE doc_chunks WHERE project_id = $pid", {"pid": PID})


@pytest.fixture(autouse=True)
def _stub_embeddings(monkeypatch):
    import embeddings

    async def embed_texts(texts):
        return [_stub_vec(t) for t in texts]

    monkeypatch.setattr(embeddings, "embed_texts", embed_texts)
    monkeypatch.setattr(
        embeddings, "_ensure_config", AsyncMock(return_value="http://e.test"))


async def _seed_fact(
    pid: str, title: str, body: str, *, embedding: list[float] | None = None,
) -> str:
    """A live memory fact-doc plus the doc_chunk that makes it reachable by the
    nearest-fact query (the gate's candidate reach).

    `embedding` defaults to the body's stub vector; pass one explicitly to desync the
    STORED chunk vector from a fresh re-embedding of the body (the lever the
    stored-index test uses)."""
    roles = await ensure_agent_system_docs(pid)
    folder = roles["memory_folder"]
    db = await get_db()
    doc_id = f"dg-{uuid4().hex[:8]}"
    await create_record("documents", doc_id, {
        "project_id": pid, "parent_id": folder,
        "title": title, "content": body,
        "path": doc_id, "is_memory": True, "mem_active": True,
        "mem": {"merge_count": 0, "version_history": [],
                "provenance": {"run_id": "seed", "sources": []}},
    })
    vec = embedding if embedding is not None else _stub_vec(body)
    await db.query(
        "CREATE type::record('doc_chunks', $cid) SET document_id = $did, "
        "project_id = $pid, ord = 0, heading = '', content = $t, offset_start = 0, "
        "offset_end = 0, content_version = 1, kind = 'memory', embedding = $vec",
        {"cid": str(uuid4()), "did": doc_id, "pid": pid, "t": body, "vec": vec},
    )
    return doc_id


async def _apply(pid, uid, verdicts):
    from memory.apply import apply_memory_verdicts
    from memory.run_key import mint_memory_run_key

    run = await mint_memory_run_key(user_id=uid, project_id=pid)
    return await apply_memory_verdicts(
        project_id=pid, run_id=run.run_id, verdicts=verdicts,
        reference_id=await ensure_memory_reference(pid),
    )


async def test_a_new_fact_restating_a_stored_one_is_rejected(project_with_doc):
    pid, _idx, uid = project_with_doc
    twin_id = await _seed_fact(pid, "Тема: оригинал", f"{_SAME} исходная формулировка")
    from fastapi import HTTPException

    # A batch whose ONLY verdict is refused applied nothing — the pinned contract is
    # a 400 (a 200 carrying an empty result reads as success).
    with pytest.raises(HTTPException) as exc:
        await _apply(pid, uid, [
            {"action": "new", "title": "Тема: пересказ",
             "text": f"{_SAME} пересказ той же мысли"},
        ])
    assert exc.value.status_code == 400
    msg = exc.value.detail
    # The message must name the twin's fact_id (so a merge can target it) and the
    # action that resolves it.
    assert twin_id in msg
    assert "merge" in msg


async def test_the_gate_is_in_a_mixed_batch_reported_in_rejected(project_with_doc):
    """A valid verdict alongside a refused one applies; the refusal comes back in
    `rejected` with the correction — not a whole-batch 400."""
    pid, _idx, uid = project_with_doc
    await _seed_fact(pid, "Тема: оригинал", f"{_SAME} уже есть")
    res = await _apply(pid, uid, [
        {"action": "new", "title": "Тема: другое", "text": f"{_OTHER} совсем иное"},
        {"action": "new", "title": "Тема: дубль", "text": f"{_SAME} повтор"},
    ])
    assert res["actions"]["new"] == 1  # the distinct one applied
    assert len(res["rejected"]) == 1
    # Uniform rejection format: preflight prefixes EVERY gate's reason with the
    # verdict index — the duplicate gate's no less than the staleness gate's.
    assert res["rejected"][0]["reason"].startswith("verdict 1: ")
    assert "merge" in res["rejected"][0]["reason"]


async def test_the_gate_fails_open_when_embeddings_are_unavailable(project_with_doc, monkeypatch):
    """Any embedding failure WRITES the fact (a duplicate is repairable by a later
    merge; a fact never written is gone)."""
    pid, _idx, uid = project_with_doc

    async def boom(_texts):
        raise RuntimeError("embeddings down")

    import embeddings

    monkeypatch.setattr(embeddings, "embed_texts", boom)
    res = await _apply(pid, uid, [{"action": "new", "title": "Т: ф", "text": "x"}])
    assert res["actions"]["new"] == 1
    assert res["rejected"] == []


async def test_the_gate_judges_by_the_stored_index_not_a_re_embedding(project_with_doc):
    """The cosine the gate judges by is the stored chunk vector's — the SAME index the
    merge-candidate surface reads — not a fresh re-embedding of the candidate body.

    The stored fact's BODY carries a marker the stub maps away from the new text, so a
    gate that re-embedded the body would score it at 0 and let the duplicate through.
    Its STORED chunk vector is pinned to the new text's vector, so a gate that reads the
    index refuses it. The desync is the test's lever, not a claim about production
    (stored and fresh agree there); it pins which source of truth the gate trusts
    against a future re-introduction of candidate re-embedding."""
    pid, _idx, uid = project_with_doc
    new_text = "новый текст без маркера"  # stub → [0, 0, 1, 0]
    body = f"{_OTHER} тело с иным маркером"  # body's OWN fresh embed → [0, 1, 0, 0]
    twin_id = await _seed_fact(
        pid, "Тема: факт", body, embedding=_stub_vec(new_text),  # pinned to the new text
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _apply(pid, uid, [
            {"action": "new", "title": "Т: дубль", "text": new_text},
        ])
    assert twin_id in exc.value.detail


async def test_two_twins_in_one_batch_are_caught(project_with_doc):
    """Intra-batch blind spot (D3): two `new` verdicts that restate EACH OTHER in the
    SAME batch. Neither is stored when the stored-index pass scores them, so today both
    apply and read as two distinct facts. The pairwise pass over the already-embedded
    vectors must refuse the later twin — one survives, one is rejected naming its sibling
    (NOT a stored fact_id to merge into — there is none yet)."""
    pid, _idx, uid = project_with_doc
    res = await _apply(pid, uid, [
        {"action": "new", "title": "Тема: первое", "text": f"{_SAME} формулировка один"},
        {"action": "new", "title": "Тема: то же", "text": f"{_SAME} формулировка два"},
    ])
    # One twin survives; the other is refused — a 200-with-rejection, not a 400 (a valid
    # verdict applied), so the batch is correctable incrementally.
    assert res["actions"]["new"] == 1
    assert len(res["rejected"]) == 1
    reason = res["rejected"][0]["reason"]
    # The message names the sibling verdict in THIS batch and must NOT direct the agent
    # to `merge` — there is no stored fact_id to merge into, so saying merge would point
    # at an id that does not exist yet.
    assert "batch" in reason.lower()
    assert "merge" not in reason.lower()


async def test_a_cluster_of_three_twins_in_one_batch_leaves_one(project_with_doc):
    """Three restatements of the same knowledge in ONE batch: one survives, two refused.
    The canonical twin is the EARLIEST surviving verdict — a later twin names it, so a
    refused verdict is never itself treated as the canonical twin for a still-later one."""
    pid, _idx, uid = project_with_doc
    res = await _apply(pid, uid, [
        {"action": "new", "title": "Тема: a", "text": f"{_SAME} вариант a"},
        {"action": "new", "title": "Тема: b", "text": f"{_SAME} вариант b"},
        {"action": "new", "title": "Тема: c", "text": f"{_SAME} вариант c"},
    ])
    assert res["actions"]["new"] == 1
    assert len(res["rejected"]) == 2


async def test_a_stored_refusal_takes_precedence_over_intra_batch(project_with_doc):
    """When verdict 0 twins a STORED fact AND verdict 1 twins verdict 0, the stored-index
    pass refuses BOTH (each twins the stored fact independently) — the intra-batch pass
    only ever sees verdicts the stored pass let through. This pins the ORDER: stored-index
    first, intra-batch among survivors."""
    pid, _idx, uid = project_with_doc
    await _seed_fact(pid, "Тема: хранимое", f"{_SAME} уже в памяти")
    from fastapi import HTTPException

    # Both verdicts restate the stored fact → a batch whose every verdict is refused is a
    # 400, regardless of how the intra-batch pass would have scored them against each other.
    with pytest.raises(HTTPException) as exc:
        await _apply(pid, uid, [
            {"action": "new", "title": "Тема: x", "text": f"{_SAME} повтор x"},
            {"action": "new", "title": "Тема: y", "text": f"{_SAME} повтор y"},
        ])
    assert exc.value.status_code == 400
