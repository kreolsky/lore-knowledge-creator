"""Per-key Redis edit lock for the agent edit apply paths — cross-replica.

# SYSTEM: doc-edit-lock — a Redis SET NX EX per-key lock that serializes the
# read-compute-write cycle of agent edit apply paths across ALL replicas, so
# parallel mutations to the same key do not race into a lost-update.

# ARCH: WHY THIS EXISTS. The agent edit apply path (apply_edit_to_document,
# _apply_edit_proposal, apply_edit_table_cell) is a non-atomic read-compute-write:
# it resolves the live content C0 (no lock), computes new_content = splice, then
# writes via route_document_content. The collab session's `_write_lock`
# (apply_external_content_change) guards only the Y.Doc mutation, NOT the
# read+compute — so two concurrent applies both read the same C0 and the last
# writer wins (earlier mutations lost; every call still returns
# {"status":"applied"} with no 409). This lock wraps the FULL resolve→splice→
# checkpoint→write so concurrent same-key applies serialize: the second reads the
# FIRST's result and re-resolves against it (the existing re-resolve / drift
# handling then applies cleanly).
#
# Cross-replica this is now backed by Redis (SET NX EX + fencing token +
# Lua compare-and-delete, mirroring turn_lock.py): two REST writes to one
# document on DIFFERENT replicas serialize through the same Redis key. The
# previous in-process asyncio.Lock registry could not reach them — reproduced
# live on the two-replica stack: concurrent distinct-anchor edits to one doc
# lost one edit in 5/10 rounds while both callers received 200 "applied".
#
# Same-key concurrent mutators this protects, on any replica:
#   - several edit_document calls in one agent turn (Fix B makes them sequential at
#     the driver, but this is defense-in-depth if Fix B is ever bypassed);
#   - two chat sessions editing the same doc;
#   - an agent edit + a browser-tab edit.
#
# KEYSPACE: the key is an opaque caller-supplied string — a document id on the
# edit/table paths, or "memory:{project_id}" on the memory-apply path — so the
# Redis prefix ("editlock:") names the LOCK, not the locked thing.
#
# REGISTRY: the old module kept a process-local dict keyed by doc_id (guarded
# create, no eviction). The Redis key replaces it: creation is the SET itself
# (atomic, no guarded-create subtlety) and eviction is the TTL.

# INVARIANT(corruption): the lock nests AROUND session._write_lock (doc_edit_lock
# is acquired first, then route_document_content → apply_external_content_change →
# session._write_lock).  Why: lock ordering must be consistent (doc_edit_lock before
# session._write_lock) to avoid deadlock; no code path acquires session._write_lock
# first and then doc_edit_lock, so there is no A-B / B-A deadlock — audit before
# nesting the locks in reverse.

# INVARIANT(corruption): release is a fencing-token compare-and-delete, NOT a bare
# DELETE. Why: if an apply outlives the TTL (slow I/O, a wedged replica), the lock
# expires and the next mutator legitimately acquires it; a bare DELETE on the slow
# holder's finish would then erase the NEW holder's lock, letting a third mutator
# in — the exact race this module exists to close. The per-acquire random token +
# Lua compare-and-delete guarantee a release only ever removes THIS holder's lock.

# INVARIANT(data-loss): Redis unavailable during acquire RAISES — the apply never
# proceeds unlocked. Why: this lock's whole purpose is preventing silent data loss
# behind a success response; running the body without the lock would re-open the
# defect exactly when the service is unhealthy. Consistent with the service-wide
# stance (backplane: Redis down = service down — no in-process fallback).

# INVARIANT(corruption): acquire is CEILINGED — past DOC_EDIT_LOCK_ACQUIRE_CEILING_S
# it raises EditLockTimeout instead of polling on. Why: an unbounded poll turns the
# two failure modes this module must stay loud about into silence. Re-entering the
# lock used to deadlock instantly (caught in seconds); with a Redis lock it would
# instead wait out the TTL and then RUN THE BODY UNPROTECTED — the lost-update the
# module exists to close, now on a timer. And under sustained same-key contention a
# polling waiter has no fairness (the asyncio.Lock woke waiters FIFO), so a waiter
# can be beaten indefinitely with nothing observable. The ceiling is 2x the TTL: any
# legitimate wait is at most one dead holder's TTL, so crossing it means a bug, not
# load.

# NON-REENTRANCY (changed character vs the asyncio version): re-acquiring the same
# key inside its own critical section does NOT deadlock — it raises EditLockTimeout
# at the ceiling above. Slower to notice than a deadlock, still loud; no code path
# re-enters (the apply cores are lock-free under it — see _locked_edit_core's WHY).
"""

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager

import redis_pool

logger = logging.getLogger(__name__)

# TTL sizing (measured, not guessed — plan replica-independent-guarantees): the
# locked span is checkpoint write + convergence write; edit_document e2e
# (RBAC + resolve + checkpoint + write, a superset of the locked span) measured
# max 0.071s over 20 sequential applies on the gray stack. 30s is a ~400x
# margin — a replica dying mid-apply wedges the document for at most 30s, and
# no realistic apply approaches the ceiling, so no heartbeat extend is wired
# (turn_lock's compare-and-extend is the shape to copy if that ever changes).
DOC_EDIT_LOCK_TTL_S = int(os.environ.get("DOC_EDIT_LOCK_TTL_S", "30"))

# Contention poll: how often a blocked acquire retries SET NX EX. The old
# asyncio.Lock woke waiters on release; polling trades up to this much latency
# on the contention loser for a stateless (multi-replica-safe) wait.
_ACQUIRE_POLL_S = 0.05

# Ceiling on a single acquire — see the INVARIANT above. 2x TTL: one dead holder's
# key lapses within TTL, so a wait past twice that is never legitimate contention.
DOC_EDIT_LOCK_ACQUIRE_CEILING_S = DOC_EDIT_LOCK_TTL_S * 2


class EditLockTimeout(TimeoutError):
    """Acquire exceeded DOC_EDIT_LOCK_ACQUIRE_CEILING_S — re-entry or starvation.

    A TimeoutError subclass so callers that already map timeouts keep working; the
    apply paths let it propagate (a 500 is the correct answer to "we cannot prove
    this write is safe" — see the data-loss INVARIANT).
    """


_LOCK_KEY_PREFIX = "editlock:"

# Lua: delete only if the stored value equals THIS holder's token (fencing).
# Atomic in Redis — no GET/DEL race. Same script as turn_lock's release.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


@asynccontextmanager
async def edit_lock(key: str, *, ttl: int = DOC_EDIT_LOCK_TTL_S):
    """Serialize the read-compute-write cycle for `key` across ALL replicas.

    A handle constructor, not an acquire: the acquisition happens in
    __aenter__ (SET NX EX with a per-acquire random token, retried every
    _ACQUIRE_POLL_S while another holder holds the key), so the contention
    loser waits here and then re-resolves through its caller's existing drift
    handling — the same behaviour the process-local asyncio.Lock had, now
    holding across replicas. The TTL bounds the wait when a holder dies
    mid-apply (its key lapses; the next poll acquires).

    Raises (never proceeds unlocked) if Redis is unreachable — see the
    data-loss INVARIANT above — or EditLockTimeout if the wait crosses
    DOC_EDIT_LOCK_ACQUIRE_CEILING_S. Release is best-effort fenced delete; a
    failed release leaves the key to the TTL.
    """
    token = await _acquire(key, ttl)
    try:
        yield
    finally:
        await _release(key, token)


async def _acquire(key: str, ttl: int) -> str:
    r = await redis_pool.get_redis()
    rkey = f"{_LOCK_KEY_PREFIX}{key}"
    token = secrets.token_urlsafe(16)
    deadline = time.monotonic() + DOC_EDIT_LOCK_ACQUIRE_CEILING_S
    while True:
        # SET key token NX EX ttl — single atomic round-trip; the token is the
        # value, so release can distinguish holders (fencing).
        ok = await r.set(rkey, token, nx=True, ex=ttl)
        if ok:
            return token
        if time.monotonic() >= deadline:
            raise EditLockTimeout(
                f"edit lock {key!r} not acquired within "
                f"{DOC_EDIT_LOCK_ACQUIRE_CEILING_S}s (re-entry or starvation)"
            )
        await asyncio.sleep(_ACQUIRE_POLL_S)


async def _release(key: str, token: str) -> None:
    r = await redis_pool.get_redis()
    rkey = f"{_LOCK_KEY_PREFIX}{key}"
    try:
        await r.eval(_RELEASE_SCRIPT, 1, rkey, token)
    except Exception:  # noqa: BLE001 — release is best-effort; TTL reaps the key
        logger.warning("edit-lock release failed for %s", key, exc_info=True)
