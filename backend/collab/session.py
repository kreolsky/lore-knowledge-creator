"""CollabSession and ConnectedClient — in-memory state for collaborative editing sessions."""
# ARCH: One CollabSession per entity — all users share one Y.Doc replica.
# ARCH: Backend-driven periodic flush (1s) — derives content from Y.Doc, persists to DB.
# ARCH: Broadcast batching (50ms) coalesces rapid updates into fewer WS sends.
# ARCH: Binary Yjs sync replaces OT — updates are applied to the pycrdt Doc directly.
# ARCH: The flush→backup→mention pipeline lives in FlushPipeline (flush_pipeline.py);
#   CollabSession holds a _flush_pipeline and delegates flush_to_db/force_flush to it. The
#   pipeline owns the flush-private state + _flush_lock; this class keeps CRDT sync,
#   client/broadcast state, zombie-reaping, and the _write_lock that serializes Y.Doc
#   mutation. Extraction is a behavior-preserving move (lock ordering unchanged).
# SYSTEM: collab-session — per-entity WebSocket sessions with CRDT Y.Doc and periodic flush

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from backplane import YDOC_CHANNEL_PREFIX, get_backplane
from fastapi import WebSocket
from pycrdt import Doc, Text

from collab.flush_pipeline import FlushPipeline
from collab.sync import (
    MSG_AWARENESS,
    MSG_SYNC,
    MSG_SYNC_STEP1,
    MSG_SYNC_UPDATE,
    handle_sync_message,
    unwrap_binary,
    wrap_binary,
)
from config import BATCH_INTERVAL, FLUSH_INTERVAL_SEC

logger = logging.getLogger(__name__)

# Plan 1.1 — chronic-failure visibility. Every best-effort guard in this module
# (WS sends, periodic loop, safe closes, telemetry records, first-edit logs)
# increments a monotonic counter exposed on /api/health. Why: each site logs its
# failure, but a log line alone is invisible to monitoring; a climbing counter is the
# tripwire (mirrors event_bus.backplane_publish_failures).
_collab_session_failures = 0


def collab_session_failures() -> int:
    """Total best-effort session-guard failures since process start (for /api/health)."""
    return _collab_session_failures


async def _safe_ws_close(ws: WebSocket, *, code: int, reason: str = "") -> None:
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        # WHY: best-effort close — the client may already be gone; never mask the caller's path.
        global _collab_session_failures
        _collab_session_failures += 1


MAX_COLLAB_SESSIONS = 500
# INVARIANT(data-loss): durability floor is the append-only ydoc_updates log, NOT this flush.
# Every received Yjs update is appended synchronously on receive (publish_doc_update,
# append=True → append_update), so the max data loss on SIGKILL is ~0 committed edits.  Why: the append-only ydoc_updates log is synchronous on receive; flush is periodic, not the floor
# FLUSH_INTERVAL_SEC only governs how often the derived content + ydoc_state SNAPSHOT
# is written (a read-model/convenience, reconstructable from the log). Why: flush-on-
# shutdown runs only on graceful stop; the append-only log is the hard floor.
HEARTBEAT_TIMEOUT_SEC = 90
# WHY 30s and not something near the 1s tick: this bounds a WEDGE, it is not a latency
# budget — a flush under real load may legitimately be slow, and cutting a healthy one
# short would drop a snapshot for no reason.
FLUSH_TIMEOUT_SEC = 30
_HAS_PUSHED_CAP = 100


@dataclass
class ConnectedClient:
    ws: WebSocket
    user_id: str
    user_name: str
    access_level: str
    last_activity: float = field(default_factory=time.monotonic)


VALID_ENTITY_TYPES = ("doc",)


@dataclass
class CollabSession:
    """In-memory CRDT session for a collaborative document.

    INVARIANT: the Y.Doc is the authoritative state while at least one client
    is connected. Content is derived from the Y.Doc on flush.  Why: live clients hold the Y.Doc in memory; flush only persists it
    """

    entity_type: str
    entity_id: str
    ydoc: Doc
    _dirty: bool = field(default=False, repr=False)
    clients: dict[int, ConnectedClient] = field(default_factory=dict)
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _flush_task: asyncio.Task | None = field(default=None, repr=False)
    _pending_broadcasts: list[tuple[bytes | dict, set[WebSocket]]] = field(default_factory=list)
    _batch_task: asyncio.Task | None = field(default=None, repr=False)
    last_editor_id: str | None = None
    last_editor_name: str | None = None
    _has_pushed: dict[str, None] = field(default_factory=dict, repr=False)
    is_reference: bool = False
    # INVARIANT(persisted): project_id is a MOVE-TIME patch target, not an immutable
    # construction fact — documents/move.py rewrites it in place when its
    # document crosses projects. Why: the flush pipeline and the embed job
    # derive doc_chunks' project_id and the project-WS nudge from THIS cached
    # value (flush_pipeline.py, embeddings.py), so an unpatched session after a
    # move persists chunks under the OLD project and nudges the wrong project WS.
    project_id: str = ""
    _backplane_subscribed: bool = field(default=False, repr=False)
    _grace_task: asyncio.Task | None = field(default=None, repr=False)
    # FlushPipeline collaborator — owns the flush→backup→mention pipeline + its private
    # state and _flush_lock (see flush_pipeline.py). Created in __post_init__ so it can
    # hold a back-reference to this session.
    _flush_pipeline: FlushPipeline = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._flush_pipeline = FlushPipeline(self)

    # ── Flush pipeline delegation ──────────────────────────────────────────────
    # The session keeps a stable public/underscore surface for the flush entry points
    # (callers in registry/dispatch/checkpoints/collab_project_ws reach them via the
    # session object). They forward to the FlushPipeline collaborator.

    async def _flush_if_needed(self, *, force: bool = False) -> None:
        await self._flush_pipeline._flush_if_needed(force=force)

    async def force_flush(self) -> None:
        """Public forced-flush entry point for callers outside the collab package."""
        await self._flush_pipeline._flush_if_needed(force=True)

    async def flush_to_db(self) -> None:
        await self._flush_pipeline.flush_to_db()

    # Backward-compat proxies for flush-private state that callers/tests touch directly.
    # The real storage lives on the pipeline; these keep the session attribute surface
    # stable (construction seeding + characterization tests) without duplicating state.
    @property
    def _last_flushed_content(self) -> str:
        return self._flush_pipeline._last_flushed_content

    @_last_flushed_content.setter
    def _last_flushed_content(self, value: str) -> None:
        self._flush_pipeline._last_flushed_content = value

    @property
    def _last_mention_ids(self) -> set[str]:
        return self._flush_pipeline._last_mention_ids

    @_last_mention_ids.setter
    def _last_mention_ids(self, value: set[str]) -> None:
        self._flush_pipeline._last_mention_ids = value

    @property
    def _updates_since_compact(self) -> int:
        return self._flush_pipeline._updates_since_compact

    @_updates_since_compact.setter
    def _updates_since_compact(self, value: int) -> None:
        self._flush_pipeline._updates_since_compact = value

    # WHY: events.apply_external_content_change (restore / agent wholesale-edit) writes
    # the baseline tables cache through the session surface so a post-restore destructive
    # edit backs up the RESTORED table state, not a stale one (INVARIANT #6). Without this
    # proxy the write is shadowed on the session and the pipeline baseline stays "{}" —
    # the loss backup then pairs baseline text with empty tables (anchor↔table-id drift).
    @property
    def _last_flushed_tables_json(self) -> str:
        return self._flush_pipeline._last_flushed_tables_json

    @_last_flushed_tables_json.setter
    def _last_flushed_tables_json(self, value: str) -> None:
        self._flush_pipeline._last_flushed_tables_json = value

    def _get_text(self) -> Text:
        from ydoc_store import get_text as _lookup
        return _lookup(self.ydoc)

    @property
    def content(self) -> str:
        return str(self._get_text())

    def add_client(self, ws: WebSocket, user_id: str, user_name: str, access_level: str) -> ConnectedClient:
        for _msg, excludes in self._pending_broadcasts:
            excludes.add(ws)
        client = ConnectedClient(
            ws=ws, user_id=user_id, user_name=user_name, access_level=access_level,
        )
        self.clients[id(ws)] = client
        return client

    def remove_client(self, ws: WebSocket) -> ConnectedClient | None:
        return self.clients.pop(id(ws), None)

    def active_users(self) -> list[dict]:
        seen: set[str] = set()
        users: list[dict] = []
        for c in self.clients.values():
            if c.user_id not in seen:
                seen.add(c.user_id)
                users.append({
                    "user_id": c.user_id,
                    "name": c.user_name,
                    # access_level lets the client filter presence to editors
                    # (full-access) for the "editing now" header signal, mirroring the
                    # server's _has_pushed discriminator. Why: a viewer/commentator must
                    # not show as "editing now"; only editors actually edit.
                    "access_level": c.access_level,
                })
        return users

    async def broadcast(self, message: dict, *, exclude_ws: WebSocket | None = None) -> None:
        text = json.dumps(message)
        for ws_id, client in list(self.clients.items()):
            if client.ws is exclude_ws:
                continue
            try:
                await client.ws.send_text(text)
            except Exception as exc:
                # WHY: a failed send means the client is gone — drop it and close; count
                # the failure so a persistent send problem is visible on /api/health.
                global _collab_session_failures
                _collab_session_failures += 1
                logger.warning("WS send failed for user=%s entity=%s: %s", client.user_id, self.entity_id, exc)
                await _safe_ws_close(client.ws, code=1011)
                self.clients.pop(ws_id, None)

    async def broadcast_binary(self, data: bytes, *, exclude_ws: WebSocket | None = None) -> None:
        for ws_id, client in list(self.clients.items()):
            if client.ws is exclude_ws:
                continue
            try:
                await client.ws.send_bytes(data)
            except Exception as exc:
                global _collab_session_failures
                _collab_session_failures += 1
                logger.warning("WS binary send failed for user=%s entity=%s: %s", client.user_id, self.entity_id, exc)
                await _safe_ws_close(client.ws, code=1011)
                self.clients.pop(ws_id, None)

    def start_periodic_flush(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._periodic_flush_loop())

    def stop_periodic_flush(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            self._flush_task = None

    async def _periodic_flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(FLUSH_INTERVAL_SEC)
                try:
                    # Snapshot-write pacing lives in the pipeline
                    # (_snapshot_pace_ok): offers are gated by the last
                    # SUCCESSFUL flush, so a failing DB keeps the 1s retry tick
                    # (fast save_degraded) while a healthy one paces the full
                    # documents-row rewrite. The reap below stays on the 1s tick.
                    # Force paths (close/leave/handoff/dispatch/shutdown) call
                    # _flush_if_needed/flush_to_db DIRECTLY and bypass pacing.
                    if await self._flush_pipeline._snapshot_pace_ok():
                        # A query on a SurrealDB connection whose _recv_task has already
                        # exited is resolved by nobody — the reader's finally cancels only
                        # the futures that existed when it died. The loop then survives its
                        # own cancel (observed: cancelling=1, still suspended) and wedges
                        # every shutdown that gathers it, with no traceback. Abandoning the
                        # flush is safe: the ydoc_updates log is the durability floor and
                        # the next tick retries; the TimeoutError is counted below.
                        # WHY the bound: an unbounded await here makes this task outlive
                        # its own cancellation and hang every shutdown that gathers it.
                        await asyncio.wait_for(self._flush_if_needed(), FLUSH_TIMEOUT_SEC)
                    await self._reap_zombie_clients()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # WHY: the periodic loop must survive any single flush/reap failure;
                    # count it so a chronically-failing loop is visible on /api/health.
                    global _collab_session_failures
                    _collab_session_failures += 1
                    logger.exception("Periodic flush loop error for %s %s", self.entity_type, self.entity_id)
        except asyncio.CancelledError:
            pass

    async def _reap_zombie_clients(self) -> None:
        now = time.monotonic()
        global _collab_session_failures
        stale = [
            (ws_id, c) for ws_id, c in self.clients.items()
            if now - c.last_activity > HEARTBEAT_TIMEOUT_SEC
        ]
        # Snapshot the doc ONCE per sweep and reuse it for every reaped editor. Why:
        # _enqueue_last_session_backup defaults to self.content (an O(n) full Y.Doc text
        # serialization); reading it once per reaped client would serialize the whole
        # document N times under a mass disconnect (N editors × O(doc)) on the collab
        # loop. Computed lazily — only when at least one reaped client is an editor.
        reaped_editors = [
            (c.user_id, c.user_name) for _, c in stale
            if c.user_id in self._has_pushed and not self.is_reference
        ]
        shared_snapshot: str | None = None
        for ws_id, client in stale:
            logger.warning(
                "Reaping zombie WS: entity=%s user=%s idle=%.0fs",
                self.entity_id, client.user_name, now - client.last_activity,
            )
            self.clients.pop(ws_id, None)
            # Upsert the editor's per-user last-session backup before the client is
            # gone. Gated on _has_pushed AND not a reference: a viewer/commentator that
            # never edited must NOT trigger a backup (mirrors the handoff discriminator),
            # and references never produce a last-session backup (the task has no
            # is_reference knowledge, so the gate must hold at the enqueue site).
            if (client.user_id, client.user_name) in reaped_editors:
                if shared_snapshot is None:
                    shared_snapshot = self.content
                await self._flush_pipeline._enqueue_last_session_backup(
                    client.user_id, client.user_name, content=shared_snapshot,
                )
            try:
                asyncio.create_task(_safe_ws_close(client.ws, code=4008, reason="Heartbeat timeout"))
            except Exception:
                # WHY: best-effort task creation — must not abort the reap sweep.
                _collab_session_failures += 1
            # Correlate the server's reap with the client's 4008 close (telemetry).
            # WHY: the WARNING above alone forces manual two-log correlation ("did the
            # server give up first or the client"). A server-reap row makes it queryable.
            # Append-only, serial (no gather on the shared Surreal conn — backend.md);
            # never let a telemetry failure break the reap.
            try:
                from telemetry_store import record_telemetry_events
                await record_telemetry_events([{
                    "category": "collab",
                    "kind": "server-reap",
                    "user_id": client.user_id,
                    "project_id": self.project_id or None,
                    "entity_id": self.entity_id,
                    "detail": {"idle_ms": round((now - client.last_activity) * 1000)},
                    "client_ts": None,
                }])
            except Exception:
                # WHY: telemetry is append-only best-effort (telemetry_store INVARIANT) —
                # never let a telemetry failure break the reap; count it all the same.
                _collab_session_failures += 1
                logger.warning("Failed to record server-reap telemetry for %s", self.entity_id, exc_info=True)
            self.queue_broadcast({"type": "user_left", "user_id": client.user_id})

    def queue_broadcast(self, message: dict | bytes, *, exclude_ws: WebSocket | None = None) -> None:
        excludes: set[WebSocket] = {exclude_ws} if exclude_ws is not None else set()
        self._pending_broadcasts.append((message, excludes))
        if self._batch_task is None or self._batch_task.done():
            self._batch_task = asyncio.create_task(self._flush_batch())

    async def _flush_batch(self) -> None:
        await asyncio.sleep(BATCH_INTERVAL)
        pending = self._pending_broadcasts[:]
        self._pending_broadcasts.clear()
        if not pending:
            return

        for msg, excludes in pending:
            for ws_id, client in list(self.clients.items()):
                if client.ws in excludes:
                    continue
                try:
                    if isinstance(msg, bytes):
                        await client.ws.send_bytes(msg)
                    else:
                        try:
                            text = json.dumps(msg)
                        except (TypeError, ValueError):
                            # INVARIANT: a non-serializable payload must be dropped, not
                            # raised — one bad broadcast must never kill the batch task.  Why: one malformed broadcast must not abort the whole batch task
                            logger.warning(
                                "Dropping non-serializable broadcast payload for entity=%s",
                                self.entity_id,
                            )
                            continue
                        await client.ws.send_text(text)
                except Exception as exc:
                    # WHY: a failed send means the client is gone — drop it and close;
                    # count it so a persistent send problem is visible on /api/health.
                    global _collab_session_failures
                    _collab_session_failures += 1
                    logger.warning("WS send failed for user=%s entity=%s: %s", client.user_id, self.entity_id, exc)
                    await _safe_ws_close(client.ws, code=1011)
                    self.clients.pop(ws_id, None)

    def _handoff_context(self, sender: ConnectedClient) -> tuple[str, str, str, str] | None:
        """Return (pre_content, prev_user_id, prev_user_name, tables_json) when this push
        is the sender's first edit this session AND a different previous editor exists.

        None = no handoff. References never hand off. Reads self.content + captures the
        tables subtree (capture_tables_json) ONLY here — i.e. only when a genuine handoff
        applies (rare: once per user/session).

        INVARIANT: handoff backup fires once per user per session, snapshotting the doc
        state BEFORE the new editor's first edit, attributed to the previous editor.
        Why: under simultaneous CRDT editing, per-switch backups thrash on interleaved
        binary updates; once-per-session is bounded (≤ #users) and simultaneity-safe.

        INVARIANT: content + tables are captured together from the SAME Y.Doc state so
        anchors and table ids stay paired (capture-timing invariant —  Why: capturing from the same Y.Doc snapshot keeps anchors paired with their table ids
        """
        if self.is_reference or sender.user_id in self._has_pushed:
            return None
        # INVARIANT(data-loss): at the _has_pushed cap, an untracked user fails CLOSED (no handoff)
        # rather than re-firing on every push. Why: the gate keys off _has_pushed
        # membership; if a user can't be recorded (cap full), firing would repeat the
        # O(n) self.content read + enqueue on each push. Skipping is bounded and safe —
        # the cap (#members in one live session) is never reached in practice.
        if len(self._has_pushed) >= _HAS_PUSHED_CAP:
            return None
        prev_id, prev_name = self.last_editor_id, self.last_editor_name
        if not prev_id or prev_id == sender.user_id:
            return None
        from table_serialize import capture_tables_json
        return self.content, prev_id, prev_name or prev_id, capture_tables_json(self.ydoc)

    async def handle_binary_message(self, data: bytes, sender_ws: WebSocket, is_multiplexed: bool) -> None:
        """Process a binary Yjs sync/awareness message from a client."""
        global _collab_session_failures
        if is_multiplexed:
            unwrapped = unwrap_binary(data)
            if unwrapped is None:
                return
            _eid, msg_type, payload = unwrapped
            # INVARIANT(corruption): the unwrapped multiplexed payload is the COMPLETE Yjs
            # message ([protocol_byte, sync_subtype, ...data]) — the frontend
            # includes the protocol byte as payload[0]. Pass it through unchanged.  Why: dropping the protocol byte corrupts the Yjs sync handshake
            # Prepending msg_type again shifts sync_subtype by one byte, so every
            # SYNC_UPDATE is misread as a STEP1 and silently dropped → edits never
            # reach the Y.Doc and content never persists.
            # Why: see lessons/2026-05-30 (multiplexed Yjs wire double-byte).
            message = payload
        else:
            if len(data) < 1:
                return
            msg_type = data[0]
            message = data

        # INVARIANT: awareness (presence cursors/selections) is RELAY-ONLY — broadcast
        # to peers, never applied to or persisted in the Y.Doc, never flagged _dirty.
        # Why: awareness bytes are not a valid Yjs update; routing them through the
        # sync path appends them to the ydoc_updates log, and apply_update() on them
        # raises ValueError on the next load() — bricking the document. It is also
        # read-only metadata, so it is allowed for every access level (incl. viewers).
        if msg_type == MSG_AWARENESS:
            self.queue_broadcast(data, exclude_ws=sender_ws)
            return

        # ARCH: Enforce write access — only 'full' may MUTATE the Y.Doc.
        # Fail-closed: unknown/unregistered clients are rejected.
        sender_client = self.clients.get(id(sender_ws))
        if not sender_client:
            return

        # WHY: a SYNC_STEP1 is a read request (the server replies STEP2 with the
        # current Y.Doc state), so it MUST be served for EVERY access level. Only
        # STEP2/UPDATE (which mutate the Y.Doc) are gated to 'full'.  Why: STEP1 only reads state; only STEP2/UPDATE mutate, so only those need 'full' access
        # Why: gating the whole sync path on write access dropped STEP1 for
        # read-only/commentator/notes clients, so they synced no content over WS and
        # opened blank until a hard reload (REST paint). See
        # lessons/2026-06-14-collab-readonly-sync-gate.md.
        is_read_request = len(message) >= 2 and message[1] == MSG_SYNC_STEP1
        if sender_client.access_level != "full" and not is_read_request:
            return

        # Compute the handoff snapshot BEFORE mutating the Y.Doc, so self.content
        # still reflects the previous editor's final state. Cheap gate — reads
        # self.content only when a genuine handoff applies. Skipped for a read
        # request, which never produces an update to broadcast.
        handoff = None if is_read_request else self._handoff_context(sender_client)

        # WHY: All Y.Doc mutations must hold _write_lock to prevent concurrent
        # corruption from binary updates, backplane, and REST paths overlapping.
        async with self._write_lock:
            response, update_to_broadcast = handle_sync_message(self.ydoc, message)

        if response is not None:
            try:
                if is_multiplexed:
                    await sender_ws.send_bytes(wrap_binary(self.entity_id, msg_type, response))
                else:
                    await sender_ws.send_bytes(response)
            except Exception:
                # WHY: best-effort sync reply — a dropped STEP2 reply is harmless; the
                # client re-requests via STEP1. Count it so a persistent send failure is
                # visible on /api/health.
                _collab_session_failures += 1

        if update_to_broadcast is not None:
            self._dirty = True

            # INVARIANT: handoff backup fires once per user per session (see
            # _handoff_context). Mark the sender as having pushed; enqueue the
            # pre-edit snapshot only on the first push when a genuine handoff applied.  Why: dedups the handoff backup to one per user per session
            # Runs OUTSIDE _write_lock — a same-user double first-push could enqueue
            # twice; the handoff: job_id + DB hash-dedup collapse it to one checkpoint.
            if sender_client.user_id not in self._has_pushed:
                # Bound the per-session set (defensive — naturally ≤ #members).
                if len(self._has_pushed) < _HAS_PUSHED_CAP:
                    self._has_pushed[sender_client.user_id] = None
                if handoff is not None:
                    pre_content, prev_id, prev_name, pre_tables = handoff
                    await self._flush_pipeline._enqueue_handoff_backup(pre_content, prev_id, prev_name, pre_tables)
                    # The previous editor's session just ended (a new editor took over)
                    # → upsert their per-user last-session from the same pre-edit capture.
                    await self._flush_pipeline._enqueue_last_session_backup(
                        prev_id, prev_name, content=pre_content, tables_json=pre_tables,
                    )
                # WHY: log first edit per user per document (idempotent via DB unique index).
                # INVARIANT: reference documents never log history events.  Why: references are derived/imported content, not user-authored history
                if not self.is_reference:
                    try:
                        from history_service import log_first_edit
                        await log_first_edit(self.entity_id, sender_client.user_id, sender_client.user_name)
                    except Exception:
                        # WHY: log_first_edit is best-effort history — a failure must not
                        # break the edit path; count it so a chronic failure is visible.
                        _collab_session_failures += 1
                        logger.debug("log_first_edit failed for doc=%s user=%s", self.entity_id, sender_client.user_id)
            self.last_editor_id = sender_client.user_id
            self.last_editor_name = sender_client.user_name

            # Log + fan out to live sessions on every replica (see publish_doc_update).
            from ydoc_store import publish_doc_update
            await publish_doc_update(self.entity_id, update_to_broadcast)
            # Track local appends for the flush-time compaction gate (see
            # _handle_flush_success) so we can skip the count() query until the
            # threshold is likely reached.
            self._updates_since_compact += 1

            # Relay the exact frame the sender sent — it is already correctly
            # framed for its channel (enveloped for multiplexed, raw otherwise).
            self.queue_broadcast(data, exclude_ws=sender_ws)

    async def apply_backplane_update(self, update_bytes: bytes) -> None:
        """Apply an update received from the backplane (another replica)."""
        # WHY: All Y.Doc mutations must hold _write_lock — see handle_binary_message.
        async with self._write_lock:
            self.ydoc.apply_update(update_bytes)
        self._dirty = True
        wrapped = wrap_binary(self.entity_id, MSG_SYNC, bytes([MSG_SYNC, MSG_SYNC_UPDATE]) + update_bytes)
        await self.broadcast_binary(wrapped)

    async def subscribe_backplane(self) -> None:
        if self._backplane_subscribed:
            return
        self._backplane_subscribed = True

        async def on_backplane_update(data: bytes):
            await self.apply_backplane_update(data)

        bp = get_backplane()
        await bp.subscribe(YDOC_CHANNEL_PREFIX + self.entity_id, on_backplane_update)

    async def unsubscribe_backplane(self) -> None:
        if not self._backplane_subscribed:
            return
        self._backplane_subscribed = False
        bp = get_backplane()
        await bp.unsubscribe(YDOC_CHANNEL_PREFIX + self.entity_id)
