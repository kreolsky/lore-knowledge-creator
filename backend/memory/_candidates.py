"""Merge-candidate selection for a portion's reference.

Leaf of task_builder: best-effort nearest-neighbour memory FACTS for the material
about to be extracted, each carrying its body so the agent judges merge-vs-new.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def _merge_candidates_for_reference(
    db, project_id: str, host_id: str, reference: dict | None,
) -> list[dict]:
    """Best-effort nearest-neighbour memory facts for the portion's reference.

    The reference is the material the agent is about to extract facts FROM, so the
    facts already semantically closest to it are the ones to merge into rather than
    re-create. Both the embedding and the query are best-effort: any miss returns []
    and the portion falls back to the flat `memory_index`. The host is excluded — it
    is never a candidate for itself. Why a bounded prefix: the reference can be a long
    transcript; only its head is needed to locate the subject, and bounding keeps the
    per-portion embed cost flat.
    """
    import embeddings
    from config import MEMORY_MERGE_CANDIDATE_QUERY_CHARS

    content = (reference or {}).get("content") or ""
    if not content.strip():
        return []
    try:
        await embeddings._ensure_config()
        # WHY: no `instruction` prefix here — this is the one embed_texts call site that
        # is a judgement, not a rule. The reference body is a DOCUMENT, not a query
        # (Qwen3 asymmetric format: a query prefix here would poison this vector). The
        # body could plausibly be phrased as a question, but embedding it as a document
        # keeps it comparable to the stored fact vectors, which are also document-
        # embedded. The rationale lived only in the embed_texts docstring; this marker is
        # where the next reader stands when reaching to add a prefix.
        vec = (await embeddings.embed_texts(
            [content[:MEMORY_MERGE_CANDIDATE_QUERY_CHARS]]
        ))[0]
    except Exception:
        # Best-effort stays (the portion falls back to the flat memory_index), but
        # NEVER silently: matches the dedup-gate and nearest-facts twins — a broken
        # embed backend shows up here in the log with its traceback.
        logger.warning(
            "merge-candidate selection unavailable, portion falls back to "
            "memory_index", exc_info=True,
        )
        return []
    ranked = await embeddings.nearest_memory_facts(
        project_id=project_id, query_vec=vec, exclude=(host_id,),
    )
    return await _attach_candidate_bodies(db, project_id, ranked)



async def _attach_candidate_bodies(
    db, project_id: str, candidates: list[dict],
) -> list[dict]:
    """Give each merge candidate its BODY — `{id, text}` — so the agent can judge
    merge-vs-new without an extra call.

    # ARCH: candidates are ranked over FACTS and adjudicated per fact, so each
    # candidate carries its own body (the fact-doc's `content`). A fact IS one fact,
    # so there is nothing to page — the whole body rides along, trimmed to id + text.

    Over-budget candidates are marked `body_deferred` and keep their id + title +
    score — never silently truncated, which is the same contract `get_memory_facts`
    # honours and for the same reason (a hidden merge candidate is the defect). The
    # first candidate is always served: it is the likeliest duplicate, and deferring it
    # would leave the list unable to do its one job.
    """
    if not candidates:
        return []
    import json

    import config

    rows = await db.query(
        "SELECT meta::id(id) AS id, content FROM documents WHERE project_id = $pid "
        "AND meta::id(id) IN $ids AND is_memory = true "
        "AND deleted_at IS NONE",
        {"pid": project_id, "ids": [c["id"] for c in candidates]},
    )
    body_by_id = {r["id"]: (r.get("content") or "") for r in (rows or [])}
    out: list[dict] = []
    total_chars = 0
    for candidate in candidates:
        body = body_by_id.get(candidate["id"], "")
        page_chars = len(json.dumps({"id": candidate["id"], "text": body}, ensure_ascii=False))
        entry = {**candidate}
        if out and total_chars + page_chars > config.MEMORY_MERGE_CANDIDATE_CHARS:
            entry["text"] = ""
            entry["body_deferred"] = True
        else:
            total_chars += page_chars
            entry["text"] = body
        out.append(entry)
    return out
