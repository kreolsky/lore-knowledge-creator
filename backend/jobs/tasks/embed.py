"""Embedding tasks — the arq half of SYSTEM: embeddings (re-embed on content_flushed,
plus the hourly coverage sweep that recovers never-embedded docs)."""
from __future__ import annotations

import logging
import random
import time
from typing import NoReturn

from jobs import pool as jobs_pool
from jobs.tasks._dead_letter import dead_letter

logger = logging.getLogger(__name__)

# WHY(one constant): this is the try budget BOTH the task and the worker
# registration must agree on — jobs/worker.py imports it for
# `func(embed_document_task, max_tries=EMBED_MAX_TRIES)` instead of re-spelling
# a literal. The task compares ctx["job_try"] against the same value to
# dead-letter on its FINAL attempt; a registration that drifted from the task's
# constant would make the task raise Retry on its last attempt straight into
# arq's silent terminal path (arq aborts with `job_try > max_tries` WITHOUT
# invoking the task — no recorder runs, no status is written: the mechanism
# behind the 187-doc prod hole of 2026-08-18).
EMBED_MAX_TRIES = 4

# Per-run enqueue cap of the hourly sweep: a cold corpus (222 docs measured on
# prod 2026-08-18) drains over several hourly cycles instead of one provider
# burst. The bound on live provider load under the cap is EMBEDDING_CONCURRENCY.
EMBED_SWEEP_CAP = 60


async def _persist_embed_failure(entity_id: str, project_id: str, reason: str) -> None:
    """Recorder shared by BOTH terminal paths (retry budget exhausted on a
    transient provider error; any other failure) — the dead_letter() contract.

    # INVARIANT(persisted): last_embed_error carries a READABLE reason, never a
    # timestamp.
    # Why: the field's NAME says error — the reason is what tells an operator
    # whether the failure is transient or content the provider will never
    # accept; updated_at already carries the time. Written as a string param so
    # it lands in the option<string> field (the schema redefines last_embed_error
    # as option<string>).
    """
    from embeddings import _on_embed_failure

    await _on_embed_failure(project_id)
    try:
        from db import get_db
        db = await get_db()
        await db.query(
            "UPDATE type::record('documents', $id) SET "
            "embedding_status = 'failed', last_embed_error = $err",
            {"id": entity_id, "err": reason},
        )
    except Exception:
        logger.warning("Failed to persist embedding_status for %s", entity_id)


async def _ahead_deadline(pool, deadline_key: str) -> float | None:
    """Seconds left on the debounce deadline if it is still meaningfully ahead,
    else None (absent, or already within the 0.5 s head-run tolerance)."""
    deadline_bytes = await pool.get(deadline_key)
    if not deadline_bytes:
        return None
    remaining = float(deadline_bytes) - time.time()
    return remaining if remaining > 0.5 else None


async def _redefer(pool, ctx: dict, remaining: float) -> NoReturn:
    """Re-defer this job behind the debounce deadline: reset arq's retry
    counter, then raise Retry.

    Called from the task HEAD (an edit storm keeps pushing the deadline back)
    and from the TAIL (an edit landed DURING the run: arq holds the job key
    until the task returns, so the edit's enqueue collapsed to None, and arq
    cannot pull a deferred job forward — the running task re-defers itself).
    A spurious re-run is a no-op write: unchanged chunks are reused by hash.
    """
    from arq.constants import retry_key_prefix
    from arq.worker import Retry

    # INVARIANT: reset arq's retry counter before re-deferring so debounce
    # re-defers do NOT count toward the budget — only genuine embed failures should.
    # Why: arq increments job_try on every run incl. Retry; a long editing session
    # would otherwise exhaust the budget and abort the embedding.
    try:
        await pool.delete(retry_key_prefix + ctx["job_id"])
    except Exception:
        pass  # WHY: best-effort retry-key cleanup — leaving it only causes a redundant future retry.
    raise Retry(defer=remaining)


async def embed_document_task(ctx, entity_type: str, entity_id: str, project_id: str) -> None:
    # Trailing debounce via re-enqueue: each content_flushed SETs embed:deadline:{id} =
    # now + cooldown and re-enqueues (dedup no-op while pending). When this deferred job
    # runs it compares now vs the deadline; if a newer edit pushed it forward, it
    # re-defers ITSELF via arq Retry instead of holding a worker slot asleep.
    deadline_key = f"embed:deadline:{entity_id}"
    pool = ctx.get("redis")
    if pool:
        remaining = await _ahead_deadline(pool, deadline_key)
        if remaining is not None:
            await _redefer(pool, ctx, remaining)
        try:
            await pool.delete(deadline_key)
        except Exception:
            pass  # WHY: best-effort deadline-key cleanup on Retry.

    import httpx

    from embeddings import _on_embed_success, _reembed

    # Derived outline refresh runs ABOVE _reembed (plan
    # document-outline-in-structure-map): _reembed returns early when
    # embeddings are unconfigured, and a project without embeddings must still
    # get outlines. Its own try — an outline failure never fails the embed job
    # (log + continue; the embed pipeline has its own failure contract below).
    try:
        from doc_outline import refresh_outline

        await refresh_outline(entity_id)
    except Exception:
        logger.warning("Outline refresh failed for %s %s", entity_type, entity_id, exc_info=True)

    try:
        await _reembed(entity_type, entity_id, project_id)
    except (httpx.HTTPStatusError, httpx.TransportError) as e:
        # A transient provider-wide failure (429 rate limit / 5xx outage, or a
        # dropped connection — TransportError: ConnectError, ReadTimeout,
        # RemoteProtocolError) must re-enqueue with backoff, NOT terminate —
        # but ONLY while tries remain. Do NOT mark embedding_status='failed'
        # and do NOT count it as a degraded-project failure while the budget
        # lasts; EMBED_MAX_TRIES bounds it. A 4xx content rejection never
        # reaches here: it is isolated per-string inside
        # _embed_texts_resilient (returns None, then the RuntimeError below
        # covers an all-rejected batch).
        if isinstance(e, httpx.TransportError):
            # No status to read — the class name is the readable failure id.
            transient = True
            label = type(e).__name__
        else:
            status = e.response.status_code if e.response is not None else 0
            transient = status == 429 or status >= 500
            label = f"HTTP {status}"
        if transient:
            if ctx["job_try"] >= EMBED_MAX_TRIES:
                # ARCH: record at the LAST try instead of relying on arq. When
                # the budget is spent arq logs 'max retries exceeded' and marks
                # the JOB failed WITHOUT invoking the task again — no recorder
                # runs, no status is written, no event fires, and the doc keeps
                # embedding_status='ok' with ZERO chunks, invisible to any
                # status-keyed sweep (measured on prod 2026-08-18: 187 such
                # docs vs 35 correctly dead-lettered). The reason names the
                # transient label (HTTP status or exception class) and the try
                # count so the record is readable. dead_letter re-raises e —
                # the Retry below is unreachable here.
                await dead_letter(
                    _persist_embed_failure(
                        entity_id, project_id,
                        f"embed failed: provider {label} persisted through "
                        f"{EMBED_MAX_TRIES} tries"[:500],
                    ),
                    e,
                )
            from arq.worker import Retry
            backoff = min(60 * 2 ** ctx["job_try"], 900)
            logger.warning(
                "Transient embed error %s for %s %s; retrying in %ds (try %d/%d)",
                label, entity_type, entity_id, backoff, ctx["job_try"], EMBED_MAX_TRIES,
            )
            raise Retry(defer=backoff)
        # Non-transient HTTPStatusError — unreachable today (see above); re-raise so arq
        # treats it as terminal rather than silently masking it as success.
        raise
    except Exception as e:
        logger.exception("Failed to reembed %s %s", entity_type, entity_id)
        # Terminal dead-letter: WHEN is dead_letter()'s contract (arq does NOT
        # auto-retry plain exceptions — this fires on the first failure). The
        # recorder marks the degraded counter + persists embedding_status='failed'
        # with a readable last_embed_error (INVARIANT inside _persist_embed_failure).
        await dead_letter(
            _persist_embed_failure(entity_id, project_id, f"embed failed: {e}"[:500]),
            e,
        )
    else:
        await _on_embed_success(project_id)
        # An edit that lands DURING the run re-defers the job: the head check
        # cannot see it (it ran before the edit) and the edit's own enqueue
        # collapsed onto this still-pending job key. Picked up HERE, after
        # _on_embed_success — the run's outcome is recorded first, the re-run
        # re-embeds (or no-ops via hash reuse) with the fresh content.
        if pool:
            remaining = await _ahead_deadline(pool, deadline_key)
            if remaining is not None:
                await _redefer(pool, ctx, remaining)


async def embed_sweep_task(ctx) -> None:
    """Hourly coverage sweep — the SAME operation as POST /embed-missing, on a
    schedule: the shared predicate, per-doc job_ids, nothing re-implemented.

    # ARCH: recovery is keyed on COVERAGE (documents_missing_embed), never on
    # embedding_status — the stored status DEFAULTS to 'ok', so a doc that was
    # never embedded (or exhausted its retries before the final-try fix) is
    # invisible to a status-keyed sweep. Measured on prod 2026-08-18: 222 docs
    # with non-empty content carried zero chunks; a 'failed'-keyed sweep would
    # have recovered 35 of them. The per-run cap drains a cold corpus over
    # several hourly cycles; arq's cron lock keeps the sweep singleton across
    # worker replicas (the pattern of the maintenance crons in jobs/worker.py).
    No per-document attempt cap: the sweep is idempotent (a doc that reaches
    'ok' leaves the set by itself) and permanently unembeddable content is
    already excluded by the predicate's CANDIDATE_WHERE + chunker check.
    """
    from embeddings import EmbeddingConfigError, _ensure_config

    try:
        await _ensure_config()
    except EmbeddingConfigError:
        # An unconfigured instance must not enqueue a per-doc no-op storm every
        # hour — and if the guard ever drifted, each task would exit at the
        # config check having burned a worker slot for nothing.
        logger.info("Embed sweep: embedding not configured, skipping")
        return

    from embedding_coverage import documents_missing_embed

    from db import get_db

    rows = await documents_missing_embed(await get_db())
    enqueued = await _enqueue_capped(rows)
    if rows:
        logger.info("Embed sweep: enqueued %d of %d missing docs", enqueued, len(rows))


async def _enqueue_capped(rows: list[dict]) -> int:
    """Post an embed per missing document, up to EMBED_SWEEP_CAP; returns how
    many LANDED.

    # WHY(shuffle): the capped slice is randomized, never the scan's leading
    # rows. The sweep has no per-document attempt cap by design, so a document
    # that keeps failing never leaves the predicate's set — under a fixed order
    # a stuck head would hold the whole cap every hour and everything past
    # EMBED_SWEEP_CAP would never be swept at all.
    # WHY(count what landed): jobs.pool.enqueue returns None when arq collapsed the
    # posting onto a pending job (a user edit already embedding this doc) —
    # that produces no provider load, and the cap exists to bound exactly that.
    """

    rows = list(rows)
    random.shuffle(rows)
    enqueued = 0
    for r in rows:
        if enqueued >= EMBED_SWEEP_CAP:
            break
        job = await jobs_pool.enqueue("embed_document_task", "doc", r["id"], r["project_id"],
                            job_id=f"embed:{r['id']}")
        if job is not None:
            enqueued += 1
    return enqueued
