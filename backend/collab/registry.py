"""Session registry — global _sessions dict and lifecycle functions."""
# SYSTEM: collab-registry — session creation, lookup, cleanup, and shutdown flush

from __future__ import annotations

import asyncio
import logging

from pycrdt import Doc

from collab.session import CollabSession
from mentions import extract_doc_mentions
from models import is_ref_row
from ydoc_store import get_text

logger = logging.getLogger(__name__)

# Plan 1.1 — chronic-failure visibility. Every best-effort guard in this module
# (ydoc load fallback, cleanup/grace/shutdown flushes, unsubscribe, compact)
# increments a monotonic counter exposed on /api/health. Why: each site logs its
# failure, but a log line alone is invisible to monitoring; a climbing counter is the
# tripwire (mirrors event_bus.backplane_publish_failures).
_collab_registry_failures = 0


def collab_registry_failures() -> int:
    """Total best-effort registry-guard failures since process start (for /api/health)."""
    return _collab_registry_failures

_sessions: dict[str, CollabSession] = {}
_create_lock = asyncio.Lock()

# WHY: a session lingers SESSION_GRACE_SEC after its last client leaves before
# teardown. Why: doc-switch churn (A→B→A within seconds) re-paid the full reload
# (ydoc_store.load + CRDT replay + subscribe_backplane) on every reopen; the grace
# window makes a quick reopen free. See Fix 1 in the perf audit.
SESSION_GRACE_SEC = 30


def _session_key(entity_type: str, entity_id: str) -> str:
    return f"{entity_type}:{entity_id}"


def _cancel_grace_teardown(session: CollabSession) -> None:
    """Cancel a pending grace-period teardown — called when a client rejoins."""
    task = session._grace_task
    if task is not None and not task.done():
        task.cancel()
    session._grace_task = None


async def _get_or_create_session(entity_type: str, entity_id: str, content: str, *, entity: dict | None = None) -> CollabSession:
    """Get an existing session or create a new one with a Y.Doc loaded from the store.

    INVARIANT: if a session already exists, returns it (content arg ignored).
    New sessions build the Y.Doc from ydoc_store or fallback to plain content.  Why: a live session already holds the authoritative Y.Doc; rebuilding from content would discard in-memory edits, so an existing session is returned as-is.
    """
    key = _session_key(entity_type, entity_id)

    # ARCH: _create_lock serializes the check-then-create sequence to prevent
    # two coroutines from both creating sessions for the same entity. The lock
    # is held only during the creation path — reads after creation are lock-free.
    existing = _sessions.get(key)
    if existing is not None:
        _cancel_grace_teardown(existing)
        return existing

    async with _create_lock:
        existing = _sessions.get(key)
        if existing is not None:
            _cancel_grace_teardown(existing)
            return existing

        ydoc = await _load_ydoc(entity_id, content)
        text = get_text(ydoc)

        initial_content = str(text)
        initial_mentions = set(extract_doc_mentions(initial_content)) - {entity_id}

        session = CollabSession(
            entity_type=entity_type,
            entity_id=entity_id,
            ydoc=ydoc,
            last_editor_id=entity.get("last_editor_id") if entity else None,
            last_editor_name=entity.get("last_editor_name") if entity else None,
            is_reference=is_ref_row(entity) if entity else False,
            project_id=(entity.get("project_id") or "") if entity else "",
        )
        # Seed the flush pipeline's baseline state from the loaded doc (proxied on the
        # session → forwarded to the FlushPipeline created in __post_init__).
        session._last_flushed_content = initial_content
        session._last_mention_ids = initial_mentions
        _sessions[key] = session

    await session.subscribe_backplane()

    return session


async def _load_ydoc(entity_id: str, fallback_content: str) -> Doc:
    """Load Y.Doc from the store, falling back to plain content."""
    try:
        from ydoc_store import load
        return await load(entity_id)
    except Exception:
        # WHY: loading the Y.Doc failing is a degraded condition (a live session would
        # otherwise serve stale content); fall back to plain content and count the
        # fallback so a chronic load failure is visible on /api/health.
        global _collab_registry_failures
        _collab_registry_failures += 1
        logger.warning("ydoc_store.load failed for %s — building from content", entity_id, exc_info=True)
        # INVARIANT(corruption): fall back through the deterministic seed, never a raw Doc() —
        # a random client_id here re-introduces the "text duplicates on re-enter"
        # bug. Why: see ydoc_store.SEED_CLIENT_ID.
        from ydoc_store import seed_doc_from_content
        return seed_doc_from_content(fallback_content or "")


async def _cleanup_session(entity_type: str, entity_id: str) -> None:
    """Called when the last client leaves. Flush immediately, then defer teardown.

    The backplane unsubscribe + compact + registry-pop are deferred to a grace timer
    so an A→B→A doc switch reuses the live session instead of rebuilding it.
    """
    key = _session_key(entity_type, entity_id)
    session = _sessions.get(key)
    if not session or session.clients:
        return

    # Immediate flush — persist the latest content right away (unchanged behavior).
    try:
        await session._flush_if_needed(force=True)
    except Exception:
        # WHY: a cleanup flush failing means the last edits may not be snapshotted on
        # disconnect; the append-only log is the durability floor, so log + count.
        global _collab_registry_failures
        _collab_registry_failures += 1
        logger.debug("Cleanup flush failed for %s", key, exc_info=True)
    if session.clients:
        return  # reconnected during the flush await — session stays fully live

    # WHY stop the tick before the grace window rather than at teardown: the flush above
    # already persisted the final state, so a loop left running through the 30s window
    # issues DB queries on behalf of nobody — and one it parks on outlives the window
    # (see FLUSH_TIMEOUT_SEC in session.py). Every join calls start_periodic_flush, so a
    # reopen within grace restarts it.
    session.stop_periodic_flush()

    if session._grace_task is None or session._grace_task.done():
        session._grace_task = asyncio.create_task(_grace_teardown(session))


async def _grace_teardown(session: CollabSession) -> None:
    """Tear down a session SESSION_GRACE_SEC after its last client left, unless a
    client rejoined in the meantime (which cancels this task via _cancel_grace_teardown).

    INVARIANT: the registry pop is synchronous and gated on an empty client set with no
    await between the check and the pop. Why: a reconnect to an existing session is
    fully synchronous (cache hit + add_client, no yield), so it either completes before
    this check (clients non-empty → abort) or runs after the pop (cache miss → fresh
    session). This closes the reconnect-during-teardown race without a lock.
    """
    global _collab_registry_failures
    try:
        await asyncio.sleep(SESSION_GRACE_SEC)
    except asyncio.CancelledError:
        return

    key = _session_key(session.entity_type, session.entity_id)
    if session.clients:
        return  # reconnected during grace — keep the live session intact
    _sessions.pop(key, None)

    session.stop_periodic_flush()
    if session._batch_task and not session._batch_task.done():
        session._batch_task.cancel()
    try:
        await session._flush_if_needed(force=True)
    except Exception:
        # WHY: a grace flush failing is a missed snapshot opportunity, not data loss
        # (append-only log is the floor); count it so a chronic failure is visible.
        _collab_registry_failures += 1
        logger.debug("Grace flush failed for %s", key, exc_info=True)
    try:
        await session.unsubscribe_backplane()
    except Exception:
        # WHY: an unsubscribe failure leaves a stray backplane subscription until the
        # process exits — harmless but count it so it does not hide silently.
        _collab_registry_failures += 1
        logger.debug("Backplane unsubscribe failed for %s", key, exc_info=True)
    try:
        from ydoc_store import maybe_compact
        await maybe_compact(session.entity_id)
    except Exception:
        # WHY: a compact failure only defers the log merge (threshold-gated, retried on
        # the next flush) — count it so a chronic failure is visible on /api/health.
        _collab_registry_failures += 1
        logger.debug("Compact failed for %s", key, exc_info=True)


async def flush_all_sessions() -> None:
    for key, session in list(_sessions.items()):
        session.stop_periodic_flush()
        _cancel_grace_teardown(session)
        if session._batch_task and not session._batch_task.done():
            session._batch_task.cancel()
        try:
            await session._flush_if_needed()
            logger.debug("Shutdown flush: %s", key)
        except Exception:
            # WHY: on shutdown a failed flush means the last snapshot is skipped; the
            # append-only log is the durability floor, so log + count the failure.
            global _collab_registry_failures
            _collab_registry_failures += 1
            logger.exception("Shutdown flush failed: %s", key)


def get_active_session(entity_type: str, entity_id: str) -> CollabSession | None:
    return _sessions.get(_session_key(entity_type, entity_id))
