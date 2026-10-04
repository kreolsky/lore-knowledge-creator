"""Minting the Memory-scoped credential a consolidation run writes under."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import redis_pool
from fastapi import HTTPException

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryRun:
    """One consolidation run.

    # ARCH (design P11 "journal-as-precondition: property taken, machinery
    # declined"): there is deliberately NO runs table. The scoped api_keys row IS
    # the run — `run_id` is its key id, and it is what each fact cites as
    # provenance. A separate table would reintroduce the second content model that
    # the whole storage decision rejected.

    `token` is the plaintext, and it is DELIBERATELY not retained by anything: the
    minting call returns it, the tool route drops it, and only its sha256 is stored.
    It is therefore unrecoverable — nobody, including us, can present this key. The
    field exists because minting produces it and because the tests use it to drive
    the wall end-to-end (the only honest proof that the wall is on the real path).

    # WHY: the verdict-apply path resolves `scope_root` by READING the key row
    # via run_id — it must never expect to hold or be handed the plaintext.
    # Why: the plaintext is gone one statement after minting, so code written on the
    # assumption that it is "kept server-side for later" fails at the moment of use,
    # and the tempting repair is to start persisting or caching it — which is what
    # turns a non-credential into a real one.
    """

    run_id: str
    token: str
    scope_root: str
    project_id: str
    user_id: str


async def mint_memory_run_key(*, user_id: str, project_id: str) -> MemoryRun:
    """Mint a fresh agent key sandboxed to the project's `Memory` folder.

    # INVARIANT(security): the returned `token` MUST NOT reach any tool result, prompt,
    # log line or document body — and MUST NOT be persisted or cached for later use.
    # Why: today the plaintext dies with this call frame, so the row is a scope +
    # provenance record rather than a live credential. Surfacing it to the model
    # would durably disclose it inside the product (the model can quote whatever it
    # is shown, into a document that is then persisted, rendered and embedded), and
    # caching it would recreate exactly the live bearer token that the row's
    # sub-day expiry would then actually matter for. Only `run_id` is safe to
    # surface, and it is all the agent needs — facts cite the run as provenance.

    # INVARIANT(security): mint a FRESH key per run — never reuse the chat session's
    # standing key (`get_or_create_session_agent_key`).
    # Why: that key is whole-project by design (`document_id=''`), so reusing it
    # would silently drop the wall entirely; and it token-rotates on a cache miss,
    # which would break the live chat that is holding it.
    """
    from agent.keys import mint_run_key
    from agent_config import ensure_agent_system_docs

    import config

    # DEBT: a run is never explicitly closed — its key dies only when the TTL runs out.
    # Why deferred: there is no close point to hang a revocation on (the loop just stops
    # serving references; `reopen_consolidation` exists precisely to resume an exhausted
    # run, so stamping `deleted_at` on exhaustion would break resumption), and memory does
    # not need one — its plaintext dies in this frame and is unrecoverable from the hash.
    # Revocation becomes load-bearing for the FIRST consumer that hands the plaintext to a
    # foreign process (the sandbox executor): there the token is live for its whole TTL on
    # a shared host. Build `revoke_run_key` with that consumer, which has a real close
    # point, not before — a revocation helper with no call site is dead code.
    roles = await ensure_agent_system_docs(project_id)
    scope_root = roles["memory_folder"]
    token, key_id, _expires = await mint_run_key(
        user_id, project_id, scope_root,
        timedelta(seconds=config.MEM_RUN_KEY_TTL_S),
    )
    return MemoryRun(
        run_id=key_id, token=token, scope_root=scope_root,
        project_id=project_id, user_id=user_id,
    )


# ─── Per-session active-run pointer ──────────────────────────────────────────
#
# A consolidation run mints a key (run_id); the agent session that started it is recorded
# here so the loop tools (`next_reference` / `apply_memory_verdicts`) need NOT hand-copy
# a 36-char run_id into every call — one transcription slip silently lost a whole batch.
# The pointer is a HINT only: the resolved run_id still flows through `_resolve_run`,
# which reads the key row + checks project_id, so a forged or stale pointer reaches, at
# most, a run in a project the caller is already authenticated for — the security wall
# in memory/apply.py does not move.
#
# ARCH: this pointer is the pipeline-native server pointer for a miswritten
# run id, AUTHORITATIVE. The named per-verdict rejection + remedy (memory
# apply's `rejected` list, the remedied 404 details) fires only when the
# pointer yields nothing. The pointer never bypasses gates: the resolved
# run_id still flows through `_resolve_run`, which re-checks project_id.

# Redis key namespace + TTL. TTL is generous (a long deliberative session); when it
# expires a resumed session has no pointer and the loop tools 400 LOUDLY — never a
# silently wrong run. There is deliberately NO runs table (design P11 above): this is
# ephemeral session state, not a run record.
MEM_RUN_KEY_PREFIX = "mem_run:"
MEM_RUN_TTL_S = 6 * 3600


async def record_active_run(session_id: str | None, run_id: str) -> None:
    """Record a session's active consolidation run so the loop tools resolve it without
    a caller-supplied run_id. A no-op without a session_id (MCP / sessionless surface).

    Best-effort: a Redis failure is logged + swallowed — the pointer is an optimization,
    not a guarantee. Callers that omit run_id then 400 loudly (the correct fallback),
    rather than 500 on a telemetry-adjacent store.
    """
    if not session_id:
        return
    try:
        await (await redis_pool.get_redis()).set(
            f"{MEM_RUN_KEY_PREFIX}{session_id}", run_id, ex=MEM_RUN_TTL_S,
        )
    except Exception:
        logger.warning("mem-run pointer record failed for %s", session_id, exc_info=True)


async def resolve_active_run(session_id: str | None) -> str | None:
    """The session's active run_id, or None (no session / none recorded / expired /
    store error). The caller decides the loud-error shape when this is the only source."""
    if not session_id:
        return None
    try:
        return await (await redis_pool.get_redis()).get(
            f"{MEM_RUN_KEY_PREFIX}{session_id}"
        )
    except Exception:
        logger.warning("mem-run pointer read failed for %s", session_id, exc_info=True)
        return None


async def resolve_run_id(*, run_id: str | None, session_id: str | None) -> str:
    """Resolve the run_id for a loop tool: a caller-supplied run_id wins; otherwise the
    session's active run; otherwise a LOUD 400 (never a silently wrong run).

    # INVARIANT: this only changes the SOURCE of run_id, never the wall.
    # Why: `_resolve_run` (memory/apply.py) still reads scope_root from the key row and
    # re-checks project_id on every call, so a forged or stale session pointer is re-
    # validated against the same check the explicit path uses — it reaches at most a run
    # in a project the caller is already authenticated for, then is rejected if absent.
    # No rights are gained. These tools are agent-only today (MCP does not serve the loop),
    # so a sessionless caller must pass run_id.
    """
    if run_id:
        return run_id
    active = await resolve_active_run(session_id)
    if active:
        return active
    raise HTTPException(
        status_code=400,
        detail=(
            "run_id is required: no active consolidation run is recorded for this "
            "session. Call consolidate_memory first, or pass run_id explicitly."
        ),
    )


# ─── Per-run apply-batch counts (the batches-per-reference shape) ─────────────
#
# facts-per-reference is recoverable from provenance; batches-per-reference is NOT,
# because the apply path stamps nothing about HOW MANY calls made a reference — the
# quota "[5,5,5,5,5,5], one apply each" was invisible until someone read the DB by hand.
# This counts them, per run, ephemerally: a hash keyed by run_id, one field per
# reference, dying with the run (TTL = MEM_RUN_TTL_S). A HINT only — best-effort,
# fail-open — so a store outage degrades the report's `batches` field to null, never
# the run itself. Per-run (not persisted on the reference) on purpose: a reference
# re-served by a resumed run is a NEW run, and its batches must not inherit the dead
# one's count.
MEM_RUN_BATCHES_PREFIX = "mem_run_batches:"


async def incr_reference_batches(run_id: str, reference_id: str) -> int | None:
    """Count one more apply batch for (run_id, reference_id); the new count, or None if
    the store is unavailable (the report degrades, the run does not)."""
    if not run_id or not reference_id:
        return None
    try:
        r = await redis_pool.get_redis()
        key = f"{MEM_RUN_BATCHES_PREFIX}{run_id}"
        # Pipeline HINCRBY + EXPIRE into one round-trip — they have no data dependency,
        # so two awaits would be two RTTs for a counter that only needs the first result.
        pipe = r.pipeline()
        pipe.hincrby(key, reference_id, 1)
        pipe.expire(key, MEM_RUN_TTL_S)
        n, _ = await pipe.execute()
        return int(n)
    except Exception:
        logger.warning(
            "mem-run batches incr failed for %s/%s", run_id, reference_id, exc_info=True,
        )
        return None


async def reference_batch_counts(run_id: str) -> dict[str, int] | None:
    """Every reference's apply-batch count for this run ({reference_id: n}), or None if
    the store is unavailable. The closing report's batches-per-reference, sourced from
    the run itself rather than the agent's recollection."""
    if not run_id:
        return None
    try:
        r = await redis_pool.get_redis()
        raw = await r.hgetall(f"{MEM_RUN_BATCHES_PREFIX}{run_id}")
        return {str(k): int(v) for k, v in (raw or {}).items()}
    except Exception:
        logger.warning("mem-run batches read failed for %s", run_id, exc_info=True)
        return None


__all__ = [
    "MemoryRun",
    "mint_memory_run_key",
    "record_active_run",
    "resolve_active_run",
    "resolve_run_id",
    "incr_reference_batches",
    "reference_batch_counts",
]
