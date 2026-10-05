"""Semantic-search measurement probe — runs INSIDE the backend container.

Drives `retrieve_context` over a fixed query set and emits a JSON snapshot so two runs
(before / after a chunker change) can be diffed. Never writes to the DB.

A query set annotates each query with the document title that SHOULD answer it, so the
metric is the rank of that document in the hit list — not a similarity number, which is
not comparable across embedding inputs (changing what gets embedded moves every cosine).

Also emits a `coverage` block: a quality baseline measured over a partially-indexed
corpus is meaningless, so the snapshot carries the indexed share it was measured on.

Every snapshot records the MEASUREMENT CONFIG it was taken under (embedding model,
score cuts, top_k, budgets). Two snapshots taken under different configs are not
comparable — the model A/B zeroes the score cuts and widens top_k, so its snapshots
must not be diffed against ones captured at the defaults.

Usage (via run.sh):  python probe.py --queries <file.json> [--top-k N] [--budget N]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")

import config  # noqa: E402
from db import get_db  # noqa: E402
from retrieval import retrieve_context  # noqa: E402

SNIPPET_PREVIEW = 120


async def _coverage(db, project_id: str) -> dict:
    """Indexed share of the corpus + stale chunk count for one project.

    `embedding_status` cannot answer this: it DEFAULTS to 'ok' (schema.surql), so a
    document that was never embedded at all is indistinguishable from a successful one.
    Chunk presence is the only honest signal.
    """
    docs = await db.query(
        "SELECT meta::id(id) AS id, content FROM documents "
        "WHERE project_id = $p AND deleted_at IS NONE AND content != NONE",
        {"p": project_id},
    )
    substantial = [d for d in (docs or []) if len(d.get("content") or "") > 200]
    empty_ids = {d["id"] for d in (docs or []) if not (d.get("content") or "").strip()}

    grouped = await db.query(
        "SELECT document_id, count() FROM doc_chunks WHERE project_id = $p "
        "GROUP BY document_id",
        {"p": project_id},
    )
    chunked = {g["document_id"]: g["count"] for g in (grouped or [])}

    unindexed = [d for d in substantial if d["id"] not in chunked]
    stale = sum(n for did, n in chunked.items() if did in empty_ids)

    return {
        "documents_over_200_chars": len(substantial),
        "of_them_unindexed": len(unindexed),
        "unindexed_chars": sum(len(d["content"]) for d in unindexed),
        "indexed_share": round(
            1 - len(unindexed) / len(substantial), 3) if substantial else None,
        # Chunks whose parent document is now empty — the text they carry no longer
        # exists in the document, yet they are still served as hits.
        "stale_chunks_of_emptied_docs": stale,
        "total_chunks": sum(chunked.values()),
    }


async def _fingerprint(db, project_id: str) -> str:
    """Identify the corpus a snapshot was measured on.

    The dev DB is shared and mutating: it is reseeded when the `surreal` container is
    recreated, and parallel sessions run backfills against the same projects. Two
    snapshots taken across such a change are not comparable, and nothing in the numbers
    themselves reveals it — a rank that moved because the corpus grew reads exactly like
    a rank that moved because the chunker improved.

    Keyed on sorted (document id, sha256(content)) pairs. Content, NOT content_version —
    `_reembed` bumps content_version itself on every re-index, so the old key moved on
    every re-embed and `compare.py --diff` refused the exact comparison it was built to
    permit. The chunk count is dropped too: it also moves on any re-chunk, and index
    state already has its own top-level `coverage` field (total_chunks) where a backfill
    belongs. A document added/edited/removed still moves this fingerprint; only a pure
    re-embed (same content) does not.
    """
    rows = await db.query(
        "SELECT meta::id(id) AS id, content FROM documents "
        "WHERE project_id = $p AND deleted_at IS NONE",
        {"p": project_id},
    )
    pairs = sorted(
        f"{r['id']}:{hashlib.sha256((r.get('content') or '').encode()).hexdigest()}"
        for r in (rows or [])
    )
    return hashlib.sha256("|".join(pairs).encode()).hexdigest()[:16]


def _measurement_config(top_k: int | None, budget: int | None) -> dict:
    """The knobs a rank is measured under — recorded so a diff cannot silently mix arms.

    RETRIEVAL_MIN_SCORE and RETRIEVAL_SCORE_DROP_OFF are absolute/relative cosine cuts
    read from module constants, not overridable per call: different embedding models sit
    at different score distributions, so at the defaults a cross-model run measures the
    threshold rather than the model. The model A/B pins both to 0 in the environment;
    this block is what proves it did.
    """
    return {
        "embedding_model": config.EMBEDDING_MODEL,
        "min_score": config.RETRIEVAL_MIN_SCORE,
        "score_drop_off": config.RETRIEVAL_SCORE_DROP_OFF,
        "max_per_doc": config.RETRIEVAL_MAX_PER_DOC,
        "top_k": top_k if top_k is not None else "config default",
        "budget_tokens": budget if budget is not None else "config default",
    }


async def _run_query(
    project_id: str, q: dict, top_k: int | None, budget: int | None
) -> dict:
    res = await retrieve_context(
        project_id=project_id,
        user_query=q["query"],
        history=[],  # INVARIANT: empty — history would trigger the LLM query rewrite
        include_documents=True,      # and make the run non-reproducible.
        include_references=True,
        include_memory=True,
        top_k_docs=top_k,
        top_k_refs=top_k,
        top_k_memory=top_k,
        token_budget_docs=budget,
        token_budget_refs=budget,
        token_budget_memory=budget,
    )
    if res.error:
        return {**q, "error": res.error, "hits": [], "rank": None}

    hits = [
        {
            "title": h.parent_title,
            "kind": h.kind,
            "score": round(h.score, 4),
            "heading": h.heading,
            "snippet": (h.snippet or "")[:SNIPPET_PREVIEW].replace("\n", " "),
        }
        for h in res.hits
    ]
    expect = (q.get("expect") or "").lower()
    rank = next(
        (i + 1 for i, h in enumerate(hits) if expect and expect in h["title"].lower()),
        None,
    )
    # INVARIANT: a query is ranked among hits of the KIND it targets, never across kinds.
    # Why: retrieval ranks the fact level ahead of the chunk level, so every memory fact
    # that clears the score cut sits in front of every document hit — with the cut zeroed
    # for a cross-model comparison, 25 unrelated facts push the answer to rank 26 and the
    # metric stops resolving the thing under test. Measured on a Russian technical corpus: the same
    # arm reads MRR 0.23 across kinds and 0.60 within kind.
    want_kind = {"memory"} if q.get("class") == "memory" else {"document", "reference"}
    same_kind = [h for h in hits if h["kind"] in want_kind]
    rank_in_kind = next(
        (i + 1 for i, h in enumerate(same_kind) if expect and expect in h["title"].lower()),
        None,
    )
    return {
        **q, "hits": hits, "rank": rank, "rank_in_kind": rank_in_kind,
        "hit_count": len(hits), "hit_count_in_kind": len(same_kind),
    }


def _aggregate(results: list[dict]) -> dict:
    """Aggregate over `rank_in_kind` — see the INVARIANT in _run_query."""
    ranked = [r for r in results if r.get("expect")]
    found = [r for r in ranked if r["rank_in_kind"]]
    mrr = sum(1 / r["rank_in_kind"] for r in found) / len(ranked) if ranked else 0.0
    by_class: dict[str, dict] = {}
    for r in ranked:
        c = by_class.setdefault(r.get("class", "-"), {"n": 0, "hit1": 0, "hit3": 0, "miss": 0})
        c["n"] += 1
        if r["rank_in_kind"] == 1:
            c["hit1"] += 1
        if r["rank_in_kind"] and r["rank_in_kind"] <= 3:
            c["hit3"] += 1
        if not r["rank_in_kind"]:
            c["miss"] += 1
    return {
        "queries": len(ranked),
        "mrr": round(mrr, 4),
        "hit_at_1": sum(1 for r in found if r["rank_in_kind"] == 1),
        "hit_at_3": sum(1 for r in found if r["rank_in_kind"] <= 3),
        "missed": len(ranked) - len(found),
        "by_class": by_class,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", required=True)
    # None = the config default, so an unflagged run reproduces the existing snapshots.
    ap.add_argument("--top-k", type=int, default=None,
                    help="override top_k for docs/refs/memory (default: config)")
    ap.add_argument("--budget", type=int, default=None,
                    help="override the per-kind token budget (default: config)")
    args = ap.parse_args()

    with open(args.queries) as f:
        spec = json.load(f)

    project_id = spec["project_id"]
    db = await get_db()
    coverage = await _coverage(db, project_id)
    fingerprint = await _fingerprint(db, project_id)

    results = []
    for q in spec["queries"]:
        results.append(await _run_query(project_id, q, args.top_k, args.budget))

    json.dump(
        {
            "set": spec.get("name", args.queries),
            "project_id": project_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "corpus_fingerprint": fingerprint,
            "measurement": _measurement_config(args.top_k, args.budget),
            "coverage": coverage,
            "summary": _aggregate(results),
            "results": results,
        },
        sys.stdout,
        ensure_ascii=False,
        indent=2,
    )


if __name__ == "__main__":
    asyncio.run(main())
