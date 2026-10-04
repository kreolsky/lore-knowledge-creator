"""FlushPipeline — the flush→backup→mention pipeline extracted from CollabSession.

# SYSTEM: collab-session — flush pipeline: persists derived Y.Doc content, enqueues
#   content-loss / handoff / last-session backups, rebuilds mention edges, and signals
#   save_degraded / save_recovered.
# ARCH: a collaborator holding the pipeline-private state and its own _flush_lock.
#   CollabSession delegates flush_to_db / force_flush here. The session keeps the Y.Doc
#   (_write_lock) and client/broadcast state; the pipeline only reads session.content /
#   ydoc / last-editor fields and writes back via the DB. This extraction is
#   behavior-preserving (a pure move) — lock ordering is unchanged: _flush_lock serializes
#   the pipeline, _write_lock (held by the session) serializes Y.Doc mutation. Extraction
#   must not reorder these or release them across the boundary.

# INVARIANT(data-loss): durability floor is the append-only ydoc_updates log, NOT this
#   flush. Why: the log is the hard floor; this flush only paces the derived snapshot.
#   Every received Yjs update is appended synchronously on receive
#   (publish_doc_update, append=True → append_update), so the max data loss on SIGKILL is
#   ~0 committed edits. This flush only governs how often the derived content +
#   ydoc_state SNAPSHOT is written (a read-model/convenience, reconstructable from the
#   log). Why: flush-on-shutdown runs only on graceful stop; the append-only log is the
#   hard floor.
"""

from __future__ import annotations

import asyncio
import logging
import time

import config
import event_bus
from collab.timeout import (
    TIMEOUT_DB_QUERY_SEC,
    TIMEOUT_EVENT_EMIT_SEC,
    TIMEOUT_MENTION_REBUILD_SEC,
    _with_timeout,
)
from db import get_db
from jobs import pool as jobs_pool
from mentions import extract_doc_mentions

logger = logging.getLogger(__name__)

# Degraded signalling threshold: this many consecutive flush failures flip the session
# into degraded mode (one save_degraded broadcast) and write last_save_failed_at.
FLUSH_DEGRADED_THRESHOLD = 3

# Plan 1.1 — chronic-failure visibility. Every best-effort guard below increments a
# monotonic counter exposed on /api/health. Why: each site logs its failure, but a log
# line alone is invisible to monitoring; a climbing counter is the tripwire (mirrors
# event_bus.backplane_publish_failures / ydoc_store.durability_degraded_count). The
# flush_to_db failure itself is additionally recorded as a `flush_failure` telemetry row
# (see _handle_flush_failure) so the incident class is queryable, not just countable.
_flush_pipeline_failures = 0


def flush_pipeline_failures() -> int:
    """Total best-effort sub-operation failures in the flush pipeline (for /api/health)."""
    return _flush_pipeline_failures


class FlushPipeline:
    """The flush→backup→mention pipeline for a CollabSession.

    Holds the pipeline-private state (content/tables baselines, mention-id cache, backup
    job-id dedup, compact gate counter, degraded/failure counters) and the _flush_lock
    that serializes the pipeline. Receives the owning CollabSession as a dependency (NOT
    a subclass) and reads its Y.Doc / content / last-editor fields; recovery/degraded
    messages are broadcast via the session.

    INVARIANT: the Y.Doc is the authoritative state while at least one client is
    connected; content is derived from it on flush (read via session.content).
    Why: DB rows are a lagging snapshot — treating them as source-of-truth mid-session
    would clobber unflushed edits.
    """

    def __init__(self, session):
        self.session = session
        self._last_flushed_content: str = ""
        self._last_flushed_tables_json: str = "{}"
        self._last_mention_ids: set[str] = set()
        self._last_backup_job_id: str | None = None
        self._updates_since_compact: int = 0
        self._consecutive_flush_failures: int = 0
        self._degraded: bool = False
        # Pacing state for the periodic snapshot offer (see _snapshot_pace_ok).
        # None = no successful flush yet → the FIRST edit of a session flushes on
        # the next 1s tick (leading edge), before any pacing applies.
        self._last_flush_ok_monotonic: float | None = None
        # _flush_lock serializes the pipeline (see ARCH above). Only the pipeline
        # acquires it; _write_lock (on the session) serializes Y.Doc mutation separately.
        self._flush_lock: asyncio.Lock = asyncio.Lock()

    async def _snapshot_pace_ok(self) -> bool:
        """Whether the periodic loop may OFFER a snapshot write right now.

        Paced by the last SUCCESSFUL flush, not by attempts: while flushes fail
        (DB down) the gate stays open so retries keep the 1s tick and the
        save_degraded signal (3 consecutive failures) stays fast. Why pace at
        all: each flush rewrites the full documents row (content + ydoc_state
        binary) — a 1s cadence on a large doc amplifies into ~1MB/s of KV
        writes → surreal store bloat → OOM kill loop (2026-08-12/15).
        """
        if self._last_flush_ok_monotonic is None:
            return True
        min_interval_s = config.FLUSH_SNAPSHOT_MIN_INTERVAL_SEC
        return time.monotonic() - self._last_flush_ok_monotonic >= min_interval_s

    async def _flush_if_needed(self, *, force: bool = False) -> None:
        if force or self.session._dirty:
            await self.flush_to_db()

    async def flush_to_db(self) -> None:
        """Persist current Y.Doc content to DB with backup, mentions, and error recovery."""
        recovery_msg = None
        degraded_msg = None
        async with self._flush_lock:
            current_content = self.session.content
            if current_content.rstrip() == self._last_flushed_content.rstrip():
                self.session._dirty = False
                return
            # WHY: bind db before the try — get_db() itself raises when the DB is
            # down (the primary degraded path), and the except handler passes db to
            # _handle_flush_failure. Without this, that handler hit UnboundLocalError
            # and the whole degraded/recovery signalling crashed instead of running.
            db = None
            try:
                db = await get_db()
                await self._flush_content_to_db(db, current_content)
                await self._rebuild_mentions_if_needed(db, current_content)
                recovery_msg = await self._handle_flush_success(current_content)
            except Exception:
                degraded_msg = await self._handle_flush_failure(db)

        if recovery_msg:
            await self.session.broadcast(recovery_msg)
        if degraded_msg:
            await self.session.broadcast(degraded_msg)

    async def _flush_content_to_db(self, db, content: str) -> None:
        """Persist content + CRDT snapshot to DB."""
        if not self.session.is_reference:
            try:
                from content_hash import hash_content as _compute_hash

                # job_id includes the baseline hash so distinct loss events get distinct
                # attempts; identical baselines while a job is in flight dedup to one.
                # INVARIANT(corruption): pass _last_flushed_content AS-IS. It is ALWAYS a string
                # Why: coercing "" → None makes the worker derive dead pipe text (see below).
                # (default "" on first flush, set to `content` on every successful flush).
                # Coercing "" → None (the old `or None`) made the worker treat an empty
                # baseline as "unknown" and fall back to the GFM-derived documents.content
                # — turning a restored checkpoint into dead pipe text. An empty string is a
                # legitimate baseline ("nothing previously persisted to lose"); it is
                # distinct from None ("unknown, derive on the worker"). Why: see INVARIANT
                # (#6) below — the baseline is ALWAYS the raw anchor-form text.
                baseline = self._last_flushed_content
                job_id = f"backup:{self.session.entity_id}:{_compute_hash(baseline or '')}"
                # INVARIANT: skip the enqueue roundtrip when the job_id is unchanged
                # since the last real enqueue. Why: arq already dedups on job_id, but a
                # failing/retrying flush re-derives the same baseline → same job_id every
                # second; suppressing the redundant RPC saves the roundtrip with no
                # behavior change. Only set after a real enqueue (below).
                if job_id != self._last_backup_job_id:
                    # WHY: enqueue the BASELINE tables (_last_flushed_tables_json),
                    # NOT a fresh capture. Why: by now the tables map may have already
                    # mutated; a fresh capture would pair baseline text with current tables
                    # → anchor↔table-id drift. _last_flushed_tables_json is set on the SAME
                    # flush as _last_flushed_content (capture-timing invariant, plan §D).
                    await jobs_pool.enqueue(
                        "auto_backup_loss_task",
                        self.session.entity_id, content,
                        baseline_content=baseline,
                        baseline_tables_json=self._last_flushed_tables_json,
                        job_id=job_id,
                    )
                    self._last_backup_job_id = job_id
            except Exception:
                # WHY: backup enqueue is best-effort — a failing enqueue must never break
                # the flush; the counter surfaces a chronic arq failure on /api/health.
                global _flush_pipeline_failures
                _flush_pipeline_failures += 1
                logger.warning("Failed to enqueue auto_backup for %s", self.session.entity_id, exc_info=True)
        ydoc_state = self.session.ydoc.get_update()
        # Persist the GFM-expanded form of `content` (table anchors → tables) so the
        # stored `documents.content` feeding embeddings/search carries table text, not
        # opaque `![label](table:id)` anchors. The CRDT snapshot keeps the live anchors.
        from table_serialize import expand_tables
        persisted_content = expand_tables(self.session.ydoc)
        await _with_timeout(
            db.query(
                "UPDATE type::record('documents', $id) SET "
                "content = $v, ydoc_state = $state, updated_at = time::now(), "
                "last_editor_id = $leid, last_editor_name = $len, "
                "last_save_failed_at = NONE",
                {"id": self.session.entity_id, "v": persisted_content, "state": ydoc_state,
                 "leid": self.session.last_editor_id, "len": self.session.last_editor_name},
                site="collab_flush",
            ),
            TIMEOUT_DB_QUERY_SEC, "db.query (UPDATE documents)",
        )

    async def _rebuild_mentions_if_needed(self, db, content: str) -> None:
        """Rebuild mention edges if the set of mentioned IDs changed."""
        new_mentions = set(extract_doc_mentions(content)) - {self.session.entity_id}
        if new_mentions == self._last_mention_ids:
            return
        from mentions import rebuild_doc_mentions
        try:
            await _with_timeout(
                rebuild_doc_mentions(db, "documents", self.session.entity_id, content),
                TIMEOUT_MENTION_REBUILD_SEC, "rebuild_doc_mentions",
            )
        except asyncio.TimeoutError:
            return
        affected = list((new_mentions - self._last_mention_ids) | (self._last_mention_ids - new_mentions))
        if affected:
            try:
                await _with_timeout(
                    event_bus.emit("backlinks_changed_batch", document_ids=affected),
                    TIMEOUT_EVENT_EMIT_SEC, "emit(backlinks_changed_batch)",
                )
            except asyncio.TimeoutError:
                # ARCH: do NOT advance _last_mention_ids on emit timeout. Why: the
                # mentions were already rebuilt in DB above, but peers never got the
                # backlinks_changed_batch delta. Leaving the cache stale makes the next
                # flush re-run the (idempotent) rebuild and re-emit, so a transient emit
                # timeout does not permanently drop the backlinks refresh. Returning here
                # skips the `_last_mention_ids = new_mentions` below.
                return
        self._last_mention_ids = new_mentions

    async def _handle_flush_success(self, content: str) -> dict | None:
        """Reset counters, emit content_flushed. Returns recovery broadcast if was degraded."""
        global _flush_pipeline_failures
        self._last_flushed_content = content
        # Advance the snapshot pacing gate only here (on success) — see
        # _snapshot_pace_ok: failed flushes must keep retrying on the 1s tick.
        self._last_flush_ok_monotonic = time.monotonic()
        # WHY: cache the tables state captured on THIS flush alongside the
        # baseline text.
        # Why: the content-loss backup pairs _last_flushed_content with
        # _last_flushed_tables_json, so a destructive edit snapshots the BASELINE
        # table state rather than the post-edit one. Captured from the same Y.Doc state as
        # `content` to keep anchors ↔ table ids paired.
        from table_serialize import capture_tables_json
        self._last_flushed_tables_json = capture_tables_json(self.session.ydoc)
        self.session._dirty = False
        recovery_msg = None
        if self._degraded:
            recovery_msg = {"type": "save_recovered", "entity_id": self.session.entity_id}
            self._degraded = False
        self._consecutive_flush_failures = 0
        # WHY (log bound): compact the ydoc_updates log on flush success so a
        # long-lived session (continuously edited, never torn down) keeps the
        # append-only log bounded. Gated by the in-process _updates_since_compact
        # counter to AVOID a per-flush count() DB round-trip on the hot 1s path:
        # the counter increments on each local append (handle_binary_message +
        # apply_external_content_change), and only when it crosses
        # COMPACT_MIN_UPDATES do we call maybe_compact — whose own count() is the
        # authoritative gate (it sees appends from other replicas/workers too), so
        # it is still correct to call and to reset the counter afterward. Safe
        # under _flush_lock (serializes against flush) and the
        # prune-by-replayed-ids invariant in maybe_compact keeps concurrent
        # appends safe. Best-effort — compact failure must NOT break the flush
        # success path.
        from ydoc_store import COMPACT_MIN_UPDATES
        if self._updates_since_compact >= COMPACT_MIN_UPDATES:
            try:
                from ydoc_store import maybe_compact
                await maybe_compact(self.session.entity_id)
                self._updates_since_compact = 0
            except Exception:
                # WHY: compact is best-effort — a failure must NOT break the flush
                # success path; the counter surfaces a chronic compact failure.
                _flush_pipeline_failures += 1
                logger.debug("maybe_compact failed on flush for %s", self.session.entity_id, exc_info=True)
        try:
            await _with_timeout(
                event_bus.emit("content_flushed",
                      entity_type="doc",
                      entity_id=self.session.entity_id,
                      project_id=self.session.project_id,
                      is_reference=self.session.is_reference),
                TIMEOUT_EVENT_EMIT_SEC, "emit(content_flushed)",
            )
        except asyncio.TimeoutError:
            pass
        except Exception:
            # WHY: content_flushed is a nudge event — a listener failure must not fail
            # the flush; the counter surfaces a chronic listener failure.
            _flush_pipeline_failures += 1
            logger.exception("content_flushed listener failed for %s", self.session.entity_id)
        return recovery_msg

    async def _handle_flush_failure(self, db) -> dict | None:
        """Increment failure counter, enter degraded mode if threshold reached. Returns degraded broadcast."""
        logger.exception("Failed to flush collab session for %s %s", self.session.entity_type, self.session.entity_id)
        self._consecutive_flush_failures += 1
        # WHY: emit a telemetry row on every flush failure so the incident class
        # (NONE-coerce-on-UPDATE silent writes — schema INVARIANT, incident 2026-05-28)
        # is queryable in the telemetry channel, not just in logs + last_save_failed_at.
        # Sibling to the `server-reap` row in session.py. Fire-and-forget per the
        # telemetry_store INVARIANT — a telemetry failure must never break the flush.
        try:
            from telemetry_store import record_telemetry_events
            await record_telemetry_events([{
                "category": "collab",
                "kind": "flush_failure",
                "project_id": self.session.project_id or None,
                "entity_id": self.session.entity_id,
                "detail": {"consecutive_failures": self._consecutive_flush_failures},
                "client_ts": None,
            }])
        except Exception:
            logger.warning("Failed to record flush_failure telemetry for %s", self.session.entity_id, exc_info=True)
        degraded_msg = None
        if self._consecutive_flush_failures >= FLUSH_DEGRADED_THRESHOLD and not self._degraded:
            self._degraded = True
            degraded_msg = {"type": "save_degraded", "entity_id": self.session.entity_id}
            # db is None when get_db() itself failed — skip the best-effort marker
            # write; the degraded broadcast is what matters.
            if db is not None:
                try:
                    await _with_timeout(
                        db.query(
                            "UPDATE type::record('documents', $id) SET last_save_failed_at = time::now()",
                            {"id": self.session.entity_id},
                            site="collab_flush",
                        ),
                        TIMEOUT_DB_QUERY_SEC, "db.query (UPDATE documents last_save_failed_at)",
                    )
                except Exception:
                    # WHY: last_save_failed_at is a best-effort marker; the degraded
                    # broadcast is the signal that matters — but count the persistence
                    # failure so a chronically-failing marker write is not invisible.
                    global _flush_pipeline_failures
                    _flush_pipeline_failures += 1
                    logger.warning("Failed to persist last_save_failed_at for %s", self.session.entity_id)
        return degraded_msg

    async def _enqueue_handoff_backup(
        self, pre_content: str, prev_id: str, prev_name: str, tables_json: str,
    ) -> None:
        """Enqueue the off-hot-path handoff snapshot write (mirrors the loss path).

        tables_json is the captured table state from the same Y.Doc read as pre_content.
        """
        try:
            from content_hash import hash_content as _compute_hash

            await jobs_pool.enqueue(
                "auto_backup_handoff_task",
                self.session.entity_id, pre_content,
                from_user_id=prev_id, from_user_name=prev_name,
                tables_json=tables_json,
                job_id=f"handoff:{self.session.entity_id}:{_compute_hash(pre_content)}",
            )
        except Exception:
            # WHY: handoff backup is best-effort — a failure must not break the first-edit
            # path; the counter surfaces a chronic arq failure on /api/health.
            global _flush_pipeline_failures
            _flush_pipeline_failures += 1
            logger.warning("Failed to enqueue handoff backup for %s", self.session.entity_id, exc_info=True)

    async def _enqueue_last_session_backup(
        self, editor_id: str, editor_name: str, *,
        content: str | None = None, tables_json: str | None = None,
    ) -> None:
        """Enqueue the per-user last-session upsert for an editor whose session just ended.

        Runs off the hot path alongside the handoff backup. By default the snapshot is
        the live `self.session.content` + `capture_tables_json(self.session.ydoc)` (the merged
        doc, which may include others' concurrent edits) — acceptable per "the state when
        that user left". At editor-handoff the caller passes the captured `pre_content` +
        `tables_json` (the doc state BEFORE the new editor's first edit) so the previous
        editor's snapshot reflects their own final state. Why not last_editor_id: that is
        a single latest-only field, wrong when a PREVIOUS editor leaves while a newer one
        is active; the caller passes the right editor_id (and leave/reap callers gate on
        _has_pushed) — the correct attribution discriminator.
        """
        try:
            from table_serialize import capture_tables_json
            snapshot = content if content is not None else self.session.content
            snap_tables = tables_json if tables_json is not None else capture_tables_json(self.session.ydoc)
            await jobs_pool.enqueue(
                "auto_backup_last_session_task",
                self.session.entity_id, snapshot,
                editor_id=editor_id, editor_name=editor_name,
                tables_json=snap_tables,
                job_id=f"last-session:{self.session.entity_id}:{editor_id}",
            )
        except Exception:
            # WHY: last-session backup is best-effort — a failure must not break the
            # leave/reap path; the counter surfaces a chronic arq failure on /api/health.
            global _flush_pipeline_failures
            _flush_pipeline_failures += 1
            logger.warning("Failed to enqueue last-session backup for %s editor=%s",
                           self.session.entity_id, editor_id, exc_info=True)
