"""HNSW vector indexes on doc_chunks / chat_chunks: the KNN fetches return what a
brute-force cosine over the same filters returns. Real SurrealDB, seeded vectors — no embedding provider."""

import math
import random
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import settings
from helpers import EMBED_DIM

from db import get_db

INDEXES = (("doc_chunks", "doc_chunks_embedding_hnsw"),
           ("chat_chunks", "chat_chunks_embedding_hnsw"))


def _unit(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


def _rand(rng: random.Random) -> list[float]:
    return _unit([rng.gauss(0.0, 1.0) for _ in range(EMBED_DIM)])


def _near(rng: random.Random, base: list[float], noise: float) -> list[float]:
    return _unit([x + rng.gauss(0.0, noise) for x in base])


async def _assert_ready(db) -> None:
    for table, index in INDEXES:
        info = await db.query(f"INFO FOR INDEX {index} ON {table}")
        assert info["building"]["status"] == "ready", (table, info)


async def _seed_doc_chunks(db, pid: str, vecs: list[list[float]], *, kind: str,
                           model: str | None, doc_ids: list[str] | None = None) -> list[str]:
    ids = [f"vi-{uuid4()}" for _ in vecs]
    rows = [{
        "id": cid, "document_id": (doc_ids[i % len(doc_ids)] if doc_ids else f"{cid}-doc"),
        "project_id": pid, "ord": 0, "content": cid, "offset_start": 0, "offset_end": 1,
        "content_version": 0, "kind": kind, "model": model, "embedding": vec,
    } for i, (cid, vec) in enumerate(zip(ids, vecs, strict=True))]
    await db.query("INSERT INTO doc_chunks $rows", {"rows": rows})
    return ids


async def _brute_doc(db, pid: str, q: list[float], *, kind: str, model: str,
                     top_k: int, scope: str = "", params: dict | None = None) -> list[str]:
    from embeddings import MODEL_DIM_GUARD_SQL
    rows = await db.query(
        "SELECT meta::id(id) AS id, vector::similarity::cosine(embedding, $q) AS score "
        "FROM doc_chunks WHERE project_id = $pid AND kind = $kind "
        + MODEL_DIM_GUARD_SQL + scope + "ORDER BY score DESC LIMIT $k",
        {"q": q, "pid": pid, "kind": kind, "model": model, "dim": len(q), "k": top_k,
         **(params or {})},
    )
    return [r["id"] for r in rows]


async def _knn_doc(pid: str, q: list[float], *, kind: str, model: str, top_k: int,
                   scope: str = "", params: dict | None = None) -> list[str]:
    from retrieval import _kind_chunk_query
    rows = await _kind_chunk_query(
        await get_db(), pid, q, kind=kind, top_k=top_k, model=model,
        scope=scope, scope_params=params or {},
    )
    return [r["id"] for r in rows]


@pytest.fixture
async def corpus(test_db):
    """Small project A beside a big project B whose vectors sit CLOSER to the
    query — a KNN that ignored the project filter would return B's rows."""
    rng = random.Random(7)
    model = await settings.get("EMBEDDING_MODEL")
    q = _rand(rng)
    pa, pb = f"vi-a-{uuid4()}", f"vi-b-{uuid4()}"
    db = await get_db()
    a_docs = [f"vi-adoc-{i}" for i in range(10)]
    a_ids = await _seed_doc_chunks(db, pa, [_near(rng, q, 0.08) for _ in range(60)],
                                   kind="document", model=model, doc_ids=a_docs)
    a_mem = await _seed_doc_chunks(db, pa, [_near(rng, q, 0.08) for _ in range(6)],
                                   kind="memory", model=model)
    a_stale = await _seed_doc_chunks(db, pa, [_near(rng, q, 0.01) for _ in range(5)],
                                     kind="document", model="other-model")
    await _seed_doc_chunks(db, pb, [_near(rng, q, 0.02) for _ in range(400)],
                           kind="document", model=model)
    await _assert_ready(db)
    yield {"q": q, "pa": pa, "model": model, "a_ids": a_ids, "a_docs": a_docs,
           "a_mem": a_mem, "a_stale": a_stale, "rng": rng}
    await db.query("DELETE doc_chunks WHERE project_id IN $p", {"p": [pa, pb]})


# TEST-WHY: cross-process-contract — the HNSW indexes exist and read `ready` after the schema apply.
async def test_indexes_defined_and_ready(test_db):
    db = await get_db()
    for table, index in INDEXES:
        info = await db.query(f"INFO FOR TABLE {table}")
        assert index in info["indexes"], (table, info["indexes"])
        assert "HNSW DIMENSION 1024" in info["indexes"][index]
    await _assert_ready(db)


# TEST-WHY: cross-process-contract — the per-kind KNN equals brute force under every filter it shares a WHERE with (another project's closer rows, a selective kind, a subtree narrow, another model's rows), fresh and after churn.
async def test_filtered_knn_equals_brute_force_through_churn(corpus):
    c, db = corpus, await get_db()
    subtree = {"scope": "AND document_id IN $allowed_docs ",
               "params": {"allowed_docs": c["a_docs"][:3]}}
    for round_ in range(4):
        want = await _brute_doc(db, c["pa"], c["q"], kind="document", model=c["model"], top_k=25)
        got = await _knn_doc(c["pa"], c["q"], kind="document", model=c["model"], top_k=25)
        assert set(got) == set(want), f"round {round_}: KNN set differs from brute force"
        assert set(got) <= set(c["a_ids"]), "another project's or another model's row surfaced"
        # A selective kind keeps its full quota on a document-heavy project.
        mem = await _knn_doc(c["pa"], c["q"], kind="memory", model=c["model"], top_k=8)
        assert set(mem) == set(c["a_mem"]), f"round {round_}: memory starved"
        want_sub = await _brute_doc(db, c["pa"], c["q"], kind="document", model=c["model"],
                                    top_k=10, **subtree)
        got_sub = await _knn_doc(c["pa"], c["q"], kind="document", model=c["model"],
                                 top_k=10, **subtree)
        assert want_sub and set(got_sub) == set(want_sub), f"round {round_}: subtree differs"
        # A row of another model never surfaces, however close.
        wide = await _knn_doc(c["pa"], c["q"], kind="document", model=c["model"], top_k=60)
        assert not set(wide) & set(c["a_stale"]), f"round {round_}: other-model row surfaced"
        # Churn: overwrite every A chunk's vector, so the graph holds deleted nodes.
        for cid in c["a_ids"]:
            await db.query("UPDATE type::record('doc_chunks', $id) SET embedding = $v",
                           {"id": cid, "v": _near(c["rng"], c["q"], 0.08)})
    await _assert_ready(db)


# TEST-WHY: cross-process-contract — chat recall equals brute force for the caller's sessions; another user's closer chunks never take a slot.
async def test_chat_recall_equals_brute_force(test_db):
    from chat_sessions.recall_search import CHAT_RECALL_FETCH, Window, _score_chunks
    rng = random.Random(11)
    model = await settings.get("EMBEDDING_MODEL")
    q = _rand(rng)
    pid, me, other = f"vi-p-{uuid4()}", f"vi-u-{uuid4()}", f"vi-u-{uuid4()}"
    mine, theirs = [f"vi-s-{uuid4()}" for _ in range(3)], f"vi-s-{uuid4()}"
    db = await get_db()

    def rows(uid, chat_ids, n, noise):
        return [{"user_id": uid, "project_id": pid, "chat_id": chat_ids[i % len(chat_ids)],
                 "message_id": f"m-{uuid4()}", "origin_key": f"o-{uuid4()}", "role": "user",
                 "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc), "ord": 0, "content": "x",
                 "content_hash": "h", "model": model, "embedding": _near(rng, q, noise)}
                for i in range(n)]

    await db.query("INSERT INTO chat_chunks $rows",
                   {"rows": rows(me, mine, 120, 0.08) + rows(other, [theirs], 300, 0.02)})
    try:
        await _assert_ready(db)
        got = await _score_chunks(db, me, pid, {s: {} for s in mine}, q, Window())
        floor = await settings.get("RETRIEVAL_MIN_SCORE")
        want = await db.query(
            "SELECT message_id, vector::similarity::cosine(embedding, $q) AS score "
            "FROM chat_chunks WHERE user_id = $uid AND project_id = $pid "
            "AND chat_id IN $sids AND model = $model ORDER BY score DESC LIMIT $n",
            {"q": q, "uid": me, "pid": pid, "sids": mine, "model": model,
             "n": CHAT_RECALL_FETCH},
        )
        want_ids = {r["message_id"] for r in want if r["score"] >= floor}
        assert want_ids and {r["message_id"] for r in got} == want_ids
    finally:
        await db.query("DELETE chat_chunks WHERE project_id = $pid", {"pid": pid})

