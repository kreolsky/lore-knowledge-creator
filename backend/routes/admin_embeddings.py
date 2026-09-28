"""Admin embeddings routes — stats, status, embed-missing, reset."""
# ARCH: embed-missing enqueues per-doc jobs via arq. reset uses inline background task.

from __future__ import annotations

import asyncio
import logging

from embedding_coverage import chunkable_candidates
from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel
from surrealdb import AsyncSurreal

from auth import require_admin
from db import get_db, validate_record_id
from jobs import pool as jobs_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/embeddings", tags=["admin-embeddings"])

_embed_task: asyncio.Task | None = None
_embed_progress: dict = {"running": False, "operation": "embedding", "processed": 0, "total": 0, "errors": 0}


# ARCH: every stats numerator is derived from ONE population — embedding_coverage
# .chunkable_candidates, the same list /embed-missing and the hourly sweep enqueue
# from — never from its own COUNT query. Why: a subset-by-construction is the only
# thing that keeps these numbers non-negative AND drainable. Two defects came from
# a second predicate here: refs_total filtered media_type = 'markdown' while the
# numerator counted every chunk-owning reference (a chunked audio ref read
# refs_without_embeddings = -1), and a CANDIDATE_WHERE-only count reported an
# embed-only document as stale while the sweep enqueued 0. Do NOT add a media
# filter (it would hide embedded transcripts) and do not re-spell the predicate.
# Pinned by test_stats_chunked_audio_ref_is_never_negative,
# test_stats_emptied_chunked_doc_counted_on_neither_side and
# test_stats_counts_only_what_embed_missing_would_enqueue.
@router.get("/stats")
async def get_stats(_: dict = Depends(require_admin), db: AsyncSurreal = Depends(get_db)):
    doc_chunks = await db.query("SELECT count() AS c FROM doc_chunks GROUP ALL")
    # WHY: the numerators derive from THIS list — the sweep's own population —
    # never from a COUNT over CANDIDATE_WHERE. Why: a count without the chunker
    # check reported an embed-only document as "stale: 1" while /embed-missing,
    # correctly, enqueued 0 — a red number nothing could ever drain (seen on dev: a
    # 198-char doc of three ref nodes, a four-image doc on a NONE-model chunk).
    candidates = await chunkable_candidates(db)

    # WHY: counted live, never a constant — this field was hardcoded to 0, so 35
    # documents sat embedding_status='failed' with zero chunks, invisible to AI chat
    # and to the operator, for two months. A stub that always reads healthy is worse
    # than no field: silent degradation with a green light on top. Pinned by
    # test_stats_pending_reembed_counts_dead_lettered_docs (asserts both directions).
    failed_rows = await db.query(
        "SELECT count() AS c FROM documents "
        "WHERE embedding_status = 'failed' AND deleted_at IS NONE GROUP ALL"
    )

    return {
        "doc_chunks_count": doc_chunks[0]["c"] if doc_chunks else 0,
        "docs_without_embeddings": sum(
            1 for c in candidates if not c["is_reference"] and not c["embedded"]
        ),
        "refs_without_embeddings": sum(
            1 for c in candidates if c["is_reference"] and not c["embedded"]
        ),
        "pending_reembed_count": failed_rows[0]["c"] if failed_rows else 0,
        # The number the operator watches drain after an EMBEDDING_MODEL swap.
        "stale_model_count": sum(1 for c in candidates if c["stale"]),
    }


@router.get("/status")
async def get_status(_: dict = Depends(require_admin)):
    return _embed_progress


class EmbedMissingRequest(BaseModel):
    """`project_id` omitted = the whole corpus (the historical behaviour)."""

    project_id: str | None = None


async def _documents_needing_embed(db, project_id: str | None) -> list[dict]:
    """`{id, project_id}` per document with NO chunks, a FAILED last embed, or
    chunks on a stale/unknown model (the post-swap drain population).

    # ARCH: three populations, not one — never embedded (no chunks),
    # embedded-then-broken (`embedding_status = 'failed'`, which keeps its
    # now-stale chunks), and stale-model (chunks whose `model` is not the
    # current EMBEDDING_MODEL — including NONE, "unknown, pre-column"; nothing
    # re-embeds an UNEDITED document on a model swap, so the sweep must). Keying
    # on chunk absence alone made `failed` a dead end: a provider 502 left the
    # document marked failed with nothing that would ever pick it up again (two
    # entities in the 2026-08-04 consolidation run, both re-enqueued by hand). A
    # status nothing acts on is not worth recording.

    # WHY: the predicate lives in embedding_coverage.documents_missing_embed —
    # ONE home shared with the coverage instrument (.claude/scripts/embedding-
    # coverage). Why: this sweep re-spelling the SQL is how the media_type and
    # non-empty-strip filters drifted from the audit's definition in the first
    # place; the instrument's before/after is only readable if both sides derive
    # from the same code. Do not inline a copy here — that also loses the
    # INVARIANT(perf) hoist of the chunk-membership lookup, which took this
    # endpoint's query to 195s on the dev corpus.
    """
    from embedding_coverage import documents_missing_embed

    return await documents_missing_embed(db, project_id)


@router.post("/embed-missing")
async def embed_missing(
    body: EmbedMissingRequest = Body(default_factory=EmbedMissingRequest),
    _: dict = Depends(require_admin),
    db: AsyncSurreal = Depends(get_db),
):
    """Enqueue an embed for every document that has no chunks or whose last embed failed.

    # ARCH: `project_id` is optional and NOT required. Why: unscoped, this is one burst
    # over every project with unembedded documents, through the arq default queue and
    # the provider — deliberate on prod, routine per project. Requiring it would break
    # the whole-corpus repair the endpoint was built for; omitting the filter entirely
    # (the state before) made the per-project repair impossible.
    """
    from embeddings import EmbeddingConfigError, _ensure_config

    try:
        await _ensure_config()
    except EmbeddingConfigError as e:
        return {"started": False, "error": str(e)}

    doc_rows = await _documents_needing_embed(db, body.project_id)

    count = 0
    for r in (doc_rows or []):
        entity_id = r["id"]
        project_id = r["project_id"]
        await jobs_pool.enqueue("embed_document_task", "doc", entity_id, project_id,
                      job_id=f"embed:{entity_id}")
        count += 1

    return {"started": True, "count": count}


_RESET_BATCH_SIZE = 50
_RESET_TABLES = {"doc_chunks"}


@router.post("/reset-docs")
async def reset_docs(_: dict = Depends(require_admin)):
    global _embed_task
    if _embed_task and not _embed_task.done():
        return {"success": False, "error": "operation already running"}
    _embed_task = asyncio.create_task(_run_reset("doc_chunks", "reset_docs"))
    return {"started": True}


async def _run_reset(table: str, operation: str) -> None:
    global _embed_progress
    if table not in _RESET_TABLES:
        raise ValueError(f"Invalid table: {table}")
    db = await get_db()
    rows = await db.query(f"SELECT meta::id(id) AS id FROM {table}")
    ids = [r["id"] for r in (rows or [])]
    total = len(ids)
    _embed_progress = {"running": True, "operation": operation, "processed": 0, "total": total, "errors": 0}

    for i in range(0, total, _RESET_BATCH_SIZE):
        if _embed_task and _embed_task.cancelled():
            break
        batch_ids = ids[i:i + _RESET_BATCH_SIZE]
        safe_ids = [validate_record_id(rid) for rid in batch_ids]
        try:
            # WHY: match rows through meta::id(id) IN $ids with BARE ids, never
            # `id IN` with "<table>:<id>" strings — SurrealDB never equates a
            # record id with its string form, so the string form deleted nothing
            # while the endpoint still answered {started: true}. Acceptance is
            # the chunk count reaching 0, never {started} alone.
            await db.query(
                f"DELETE FROM {table} WHERE meta::id(id) IN $ids",
                {"ids": safe_ids},
            )
            _embed_progress["processed"] = min(i + _RESET_BATCH_SIZE, total)
        except Exception:
            logger.exception("Reset batch failed for %s", table)
            _embed_progress["errors"] += len(batch_ids)
            _embed_progress["processed"] = min(i + _RESET_BATCH_SIZE, total)

    _embed_progress["running"] = False

