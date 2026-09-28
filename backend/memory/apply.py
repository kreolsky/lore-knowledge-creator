"""Applying a batch of per-fact verdicts to project memory.

# ARCH: this endpoint is a TRANSACTION boundary, not a security boundary. The wall
# is the existing one (SYSTEM: scope), inherited from the run key's `scope_root`; the
# job that only this layer can do is applying a set of verdicts together and holding
# the lock.

# ARCH: memory — apply. A verdict creates or rewrites a fact DOCUMENT (`content` IS
# the fact, authored prose). There is no render step and no latch.

This module is the ORCHESTRATOR: the lock + the transaction boundary, persistence +
compensation (the all-or-nothing guarantee), and the result builder. The PURE leaves
live in sibling modules — _apply_validation (validators + pre-flight) and
_apply_resolution (fact appliers + scope-checked loading) — re-exported here.
_compensate_delete_orphans STAYS here with its callers: tests monkeypatch it on this
module's namespace, and a caller must resolve the patched name from here at call time.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

import event_bus
from db import get_db
from memory._apply_resolution import (
    _apply_verdicts,
    _resolve_run,
    _unresolvable_fact_errors,
)
from memory._apply_validation import (
    _ACTIONS,  # noqa: F401  — re-exported for white-box tests
    _preflight_verdicts,
    _tally_verdicts,
    _validate_verdict,  # noqa: F401  — re-exported for white-box tests
)
from memory.stats import log_memory_shape, memory_shape

logger = logging.getLogger(__name__)


async def _resolve_apply_inputs(
    *, project_id: str, run_id: str, reference_id: str,
    verdicts: list[dict], user: dict | None,
) -> tuple[str, dict, dict[int, str]]:
    """Resolve the run context and the pre-lock refusal map (read-only gates only).

    Both refusal sources only READ, so they run BEFORE the project lock — holding it
    across the duplicate gate's embedding round-trip (or the staleness gate's scan)
    would serialize every other run behind this one's latency. The staleness gate
    turns an unresolvable fact address into a per-verdict rejection with the
    recovery action, instead of the bare whole-batch 404 that used to fire
    mid-persistence (plan 1786753729393, layer (c) of the address-failure
    contract).
    """
    from memory.dedup import duplicate_fact_errors

    run = await _resolve_run(project_id, run_id)
    scope_root = run.scope_root
    source = await _portion_source(project_id, reference_id)
    if user is None:
        # INVARIANT(security): a memory write runs under the OWNING user's identity.
        # Why: the project-wide rule that an agent can never exceed the invoking user's
        # rights. A synthetic writer would be an identity with independent grants.
        # The user id comes off the SAME api_keys read as the scope wall — one
        # round-trip per batch, not one per resolved fact.
        from api_key_auth import _resolve_user

        user = await _resolve_user(run.user_id)
    _stamp_provenance(verdicts, source)
    dup_errors = await duplicate_fact_errors(verdicts, project_id=project_id)
    stale_errors = await _unresolvable_fact_errors(verdicts, project_id=project_id)
    # Disjoint by construction (the duplicate gate judges `new` verdicts, the
    # staleness gate merge/supersede ones), but merged rather than replaced so a
    # future gate composes instead of overwriting.
    return scope_root, user, {**dup_errors, **stale_errors}


async def apply_memory_verdicts(
    *, project_id: str, run_id: str, verdicts: list[dict], reference_id: str,
    user: dict | None = None,
) -> dict:
    """Apply a batch of per-fact verdicts under the project's memory lock.

    # ARCH: ONE lock per PROJECT. Two runs on different targets serialize where
    # per-target scoping used to let them proceed independently — accepted, because it
    # is the same lock that makes a merge safe: a merge is a read-compute-write cycle
    # over a fact and its neighbour, and two interleaved applies would race.

    # Cross-replica: the lock is the shared Redis edit lock (doc_edit_lock), so
    # applies on different replicas serialize too — ambient consolidation would
    # be safe; nothing in the current design limits it to one replica.
    """
    from doc_edit_lock import edit_lock

    scope_root, user, extra_errors = await _resolve_apply_inputs(
        project_id=project_id, run_id=run_id, reference_id=reference_id,
        verdicts=verdicts, user=user,
    )
    # Namespaced key in the shared Redis edit-lock keyspace — a uuid doc id can
    # never collide with this prefix, so reusing the lock costs nothing and
    # avoids a second lock module with the same fencing subtleties.
    async with edit_lock(f"memory:{project_id}"):
        result, written = await _apply_locked(
            project_id=project_id, run_id=run_id, verdicts=verdicts,
            scope_root=scope_root, user=user, extra_errors=extra_errors,
            reference_id=reference_id,
        )
    # WHY: each written fact is embedded HERE — in the apply that wrote it, but
    # OUTSIDE the lock. Under the lock: it is a 30 s TTL with no heartbeat while
    # an embed round-trip can run 60 s — the lock would lapse mid-embed and two
    # applies would interleave (the same reason the dedup gate's embedding
    # round-trip runs BEFORE the lock). Left to the debounce alone: the next
    # portion's dedup gate and merge candidates cannot see a fact whose vector
    # arrives a cooldown later. reembed_now never raises — a provider blip costs
    # the early vector, not the apply; the debounced job still catches up.
    from embeddings import reembed_now

    for doc_id in written:
        await reembed_now(doc_id, project_id)
    return result


def _stamp_provenance(verdicts: list[dict], source: dict) -> None:
    """# INVARIANT: provenance is STAMPED from the validated portion, never taken from
    # the verdict.
    # Why: `sources` is the only thread from a fact back to the material it came from
    # (D8 recoverability), and as an agent-filled field it was wrong in every record
    # ever written — two sessions invented two vocabularies for ids that were in fact
    # references. The server knows what it served; the agent only names WHICH portion,
    # in one typed field it cannot mistype.
    """
    for v in verdicts:
        v["sources"] = [dict(source)]


async def _portion_source(project_id: str, reference_id: str) -> dict:
    """The typed provenance entry for the portion this batch consolidated.

    Validated BEFORE the lock and before any write: an unresolvable source is worse
    than a failed batch, because the fact still reads as attested while pointing at
    nothing. Cross-project and non-reference ids are refused with the same message —
    the target is gated by the caller's project, so there is no existence oracle.
    """
    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id FROM documents WHERE meta::id(id) = $rid "
        "AND project_id = $pid AND is_reference = true AND deleted_at IS NONE",
        {"rid": reference_id, "pid": project_id},
    )
    if not rows:
        raise HTTPException(
            status_code=400,
            detail=(
                f"reference_id {reference_id!r} is not a reference in this project — "
                "pass the `id` of the reference this portion served"
            ),
        )
    return {"kind": "reference", "id": reference_id}


async def _compensate_delete_orphans(
    created: list[str], *, run_id: str, project_id: str,
) -> None:
    """Best-effort hard-delete of the freshly-created fact-docs a failed batch leaves
    behind.

    # ARCH: deletes ONLY the ids this batch created (`created`), never anything a
    # verdict targeted by `fact_id` (those predate the batch). The orphans are
    # brand-new fact-docs from a `new` verdict; a hard DELETE is the matching inverse
    # of `_create_fact`. A soft-delete is NOT used: it would leave the row visible in
    # the tree.
    #
    # LOUD + best-effort: a per-id failure logs WARNING (id + title, so the orphan is
    # findable) and continues; the caller (`_safe_compensate`) wraps this call so a
    # total failure never masks the original error.
    """
    if not created:
        return
    db = await get_db()
    for doc_id in created:
        title = ""
        try:
            rows = await db.query(
                "SELECT title FROM type::record('documents', $id)", {"id": doc_id},
            )
            title = ((rows[0] if rows else {}) or {}).get("title") or ""
            await db.query(
                "DELETE type::record('documents', $id)", {"id": doc_id},
            )
            # The matching inverse of `create_document_via_collab`, which emitted
            # `document_created`: tell WS clients to drop the empty doc from the tree,
            # or it stays visible until a refresh.

            await event_bus.emit("document_deleted", project_id=project_id, document_id=doc_id)
        except Exception:
            logger.warning(
                "apply_memory_verdicts: run %s — compensating cleanup FAILED for "
                "orphan %s (%r); remove it manually. The original error propagates.",
                run_id, doc_id, title,
            )
            continue
        logger.warning(
            "apply_memory_verdicts: run %s — deleted orphaned fact %s (%r) "
            "after a failed batch",
            run_id, doc_id, title,
        )


async def _safe_compensate(
    created: list[str], *, run_id: str, project_id: str,
) -> None:
    """Run the compensating delete, swallowing any cleanup failure so it never masks
    the original batch error. `_compensate_delete_orphans` already swallows per-id
    failures; this guard catches a total cleanup failure too and logs the orphan ids."""
    try:
        await _compensate_delete_orphans(created, run_id=run_id, project_id=project_id)
    except Exception:
        logger.warning(
            "apply_memory_verdicts: run %s — compensating cleanup ITSELF failed for "
            "orphan ids %s; remove them manually", run_id, created,
        )


async def _persist_batch_or_compensate(
    valid: list[dict], *, project_id: str, run_id: str, scope_root: str,
    user: dict,
) -> tuple[list[str], list[str]]:
    """Apply the batch as ONE all-or-nothing step over its CREATED facts.

    Returns `(created, written)` — `created` are fact-doc ids THIS batch brought into
    existence (compensated on failure); `written` is every touched fact-doc.

    # WHY: a verdict batch either applies, or leaves NONE of this batch's
    # created fact-docs behind.
    # Why: a `new` verdict creates the doc IMMEDIATELY; any raise on a later verdict
    # leaks every created fact as a live doc in the tree and the index. The
    # compensating delete hard-deletes ONLY this batch's `created` before re-raising.
    """
    created: list[str] = []
    written: list[str] = []
    try:
        # `created` is a caller-owned accumulator: _apply_verdicts appends to it as it
        # creates, so a raise on a LATER verdict still leaves the ids it must delete.
        written = await _apply_verdicts(
            valid, project_id=project_id, run_id=run_id, scope_root=scope_root,
            user=user, created=created,
        )
    except Exception:
        # INVARIANT(data-loss): compensate takes ONLY `created` — ids this batch
        # brought into existence, never a fact targeted by `fact_id`.
        # Why: a pre-existing fact carries established knowledge — deleting it on a
        # failed batch is irrecoverable loss, so the rollback is bounded to what
        # THIS batch created.
        await _safe_compensate(created, run_id=run_id, project_id=project_id)
        raise
    return created, written


async def _reference_shape(
    *, run_id: str, reference_id: str, facts: int,
) -> dict:
    """The per-reference shape for the apply result: `facts` (provenance, passed in from
    the shared `memory_shape` scan) and `batches` (the per-run counter, incremented HERE).

    # WHY: `facts` is handed in, not re-queried — the apply already paid for one
    # `memory_shape` scan that produced both the project stats and this reference's
    # count, so a second scan would double the apply's DB round-trips for one number.
    # `batches` is best-effort, fail-open (None when the store is down), resolved through
    # the `run_key` MODULE (not a bound import) so a test patching the counter is reached.
    """
    from memory import run_key

    batches = await run_key.incr_reference_batches(run_id, reference_id)
    return {"reference_id": reference_id, "facts": facts, "batches": batches}


async def _apply_locked(
    *, project_id: str, run_id: str, verdicts: list[dict], scope_root: str, user: dict,
    reference_id: str, extra_errors: dict[int, str] | None = None,
) -> tuple[dict, list[str]]:
    """The batch body, under the lock. Returns `(result, written)` — `written`
    feeds the post-lock inline embed in `apply_memory_verdicts`."""
    # `extra_errors` was decided before the lock (see `_resolve_apply_inputs`) and is
    # merged into the preflight so the batch reports every refusal together. Today it
    # carries the two read-only, pre-lock gates: the duplicate gate and the staleness
    # gate (unresolvable fact addresses).
    valid, rejected = _preflight_verdicts(verdicts, extra_errors)
    # A 400 ONLY when nothing could be applied: a 200-with-rejections is correctable
    # incrementally, a 400 is not.
    if not valid and rejected:
        raise HTTPException(400, detail="; ".join(r["reason"] for r in rejected))
    created, written = await _persist_batch_or_compensate(
        valid, project_id=project_id, run_id=run_id, scope_root=scope_root,
        user=user,
    )
    actions, skipped = _tally_verdicts(valid)
    # ONE scan serves both the project stats and this reference's fact count (the apply
    # hot path used to scan twice — once for stats, once for the per-reference number).
    shape = await memory_shape(project_id)
    stats = {"facts": shape["facts"], "retired": shape["retired"]}
    reference_shape = await _reference_shape(
        run_id=run_id, reference_id=reference_id,
        facts=shape["by_reference"].get(reference_id, 0),
    )
    result = await _build_apply_result(
        written=written, created=created, actions=actions, skipped=skipped,
        rejected=rejected, project_id=project_id, run_id=run_id, stats=stats,
        reference_shape=reference_shape,
    )
    return result, written


def _log_apply_outcome(
    *, run_id: str, created: list[str], touched: list[str], actions: dict[str, int],
    rejected: list[dict],
) -> None:
    """The batch's line in the run's log: what was written, and what was refused.

    Rejections are logged at WARNING and never folded into `skipped`: a rejection list
    the model ignores silently loses facts, and with no pending state the log is where
    a refused verdict outlives the transcript.
    """
    logger.info(
        "apply_memory_verdicts: run %s — %d created, %d touched, %s",
        run_id, len(created), len(touched),
        ", ".join(f"{n}× {a}" for a, n in actions.items() if n),
    )
    if rejected:
        logger.warning(
            "apply_memory_verdicts: run %s — %d verdict(s) REJECTED: %s",
            run_id, len(rejected),
            "; ".join(r["reason"] for r in rejected),
        )


async def _build_apply_result(
    *, written: list[str], created: list[str], actions: dict[str, int],
    skipped: list[dict], rejected: list[dict], project_id: str, run_id: str,
    stats: dict, reference_shape: dict | None = None,
) -> dict:
    """Assemble the apply result: the project-wide stats + the per-batch accounting
    (created/touched/actions/skipped/rejected) + this reference's shape (facts +
    batches, next to `stats`). `stats` is passed in (computed once with the reference
    count in `_apply_locked`) so the result builder does not trigger a second scan."""
    touched = [d for d in written if d not in created]
    _log_apply_outcome(
        run_id=run_id, created=created, touched=touched, actions=actions,
        rejected=rejected,
    )
    log_memory_shape("apply_memory_verdicts", project_id, stats)
    return {
        "run_id": run_id,
        # WHY: every id this result reports is NAMED.
        # Why: a portion's facts outlive one batch and `memory_index` refreshes per
        # PORTION, so this result is the only channel through which the agent learns
        # the id of what it just wrote; bare ids make the mapping ambiguous as soon as
        # a batch touches two facts, which pushes the agent into re-creating instead of
        # continuing — the duplication the duplicate gate exists to prevent.
        "facts": [{"id": doc_id} for doc_id in written],
        "created": created,
        "touched": touched,
        "actions": actions,
        "stats": stats,
        # The shape of the reference just consolidated: facts-per-reference (the
        # extraction quota's signal) + batches-per-reference. Sourced from the server's
        # own state, never the agent's recollection.
        "reference_shape": reference_shape,
        "skipped": skipped,
        "rejected": rejected,
    }


__all__ = ["apply_memory_verdicts"]
