/**
 * Yjs provider for multiplexed project WebSocket — CRDT-based collab.
 *
 * Manages Y.Doc per entity, binary sync via Yjs protocol, and awareness.
 * Replaces the OT-based EntityHandle and push/push_batch/transform logic.
 *
 * Concern split: binary frame codec in yjs-binary.ts; entity-generic control
 * frames in yjs-events.ts; doc-channel frames + the doc callback surface in
 * doc-callbacks.ts. The entity-kind vocabulary has ONE home (entity-types.ts)
 * — this provider stores the type per entity and rejoins from the stored
 * value, never from a string literal.
 *
 * // ARCH: Yjs sync step1/step2 handles state transfer on join/reconnect.
 * //       No version tracking, no OT, no pendingOps — CRDT converges.
 * // SYSTEM: collab — multiplexed WebSocket per project with Yjs CRDT sync
 */

import * as Y from 'yjs';
import { mergeUpdates } from 'yjs';
import { encodeAwarenessUpdate } from 'y-protocols/awareness';
import { YjsAwarenessAdapter, removeAwarenessStatesForUser, type AwarenessAdapter, type LocalPresenceUser } from './yjs-binding';
import type { WsStatus } from './ws-status';
import { ReconnectController, MAX_RECONNECT_ATTEMPTS } from './reconnect-controller';
import { isAuthCloseCode } from './ws-close-codes';
import { logCollabEvent } from './collab-log';
import { recordRtt, recordAckLatency, recordSyncLatency, recordRecovered } from '../telemetry/perf';
import {
  MSG_AWARENESS,
  MSG_SYNC,
  SYNC_STEP1,
  SYNC_STEP2,
  SYNC_UPDATE,
  wrapBinary,
  unwrapBinary,
} from './yjs-binary';
import { isString, broadcastToBundles, dispatchEntityFrame } from './yjs-events';
import { attachLocalDoc, clearLocalDoc, clearProjectLocalDocs, type LocalDocHandle } from './local-doc-persistence';
import { dispatchDocControlFrame, type DocCollabCallbacks } from './doc-callbacks';
import type { EntityType } from './entity-types';

export type { EntityType } from './entity-types';
export type { YjsCollabCallbacks, PresencePayload } from './yjs-events';
export type { DocCollabCallbacks } from './doc-callbacks';

export interface EntityYjsState {
  /** Entity kind this state was joined under — the rejoin path reads it (no literals). */
  entityType: EntityType;
  ydoc: Y.Doc;
  ytext: Y.Text;
  awareness: AwarenessAdapter;
  // WHY: event dispatch iterates `callbackBundles` so every registered consumer
  // receives events (split view, agent + editor observing the same entity). Why:
  // previously a single last-writer `callbacks` field was overwritten by a second
  // joinEntity, silently dropping the first consumer's onSynced/onUserJoined.
  callbackBundles: DocCollabCallbacks[];
  /**
   * IndexedDB mirror of this doc (see SYSTEM: local-doc-persistence). Null when the
   * browser has none, and nulled once the mirror is deleted (see _dropLocalCopy).
   */
  local: LocalDocHandle | null;
  /** True once the mirror has been applied — the join waits for it (see _joinAndSync). */
  localLoaded: boolean;
  /** Set when the doc changed while not synced — the sync pushes those edits up once. */
  hasUnsyncedLocalEdits: boolean;
  synced: boolean;
  refCount: number;
  /** Per-registration teardown — removes THIS joinEntity's bundle only (multi-consumer). */
  leave: () => void;
  disposeObserver: () => void;
  disposeAwareness: () => void;
  // SYSTEM: region-lock — reports the local selection to the backend so an agent
  // edit intersecting it is rejected. Undefined when the
  // provider has no open socket; the editor debounces + guards empty selections.
  reportSelection?: (from: number, to: number) => void;
}

export const HEARTBEAT_INTERVAL_MS = 30_000;

// The client's own liveness deadline: a heartbeat unanswered for this long means the
// server is not there, whatever the socket claims. Measured with the backend frozen
// (`docker compose pause backend`): readyState stayed OPEN, bufferedAmount stayed 0
// and the editor stayed editable for the full 150s observation — the ack is the only
// signal that crosses the wire. Sized off the recorded RTT distribution (dev p95 7ms,
// max 32ms) with orders of magnitude of headroom for a slow WAN link, because a false
// drop interrupts typing that would have been fine. Detection is therefore bounded by
// HEARTBEAT_INTERVAL_MS + this.
export const HEARTBEAT_ACK_DEADLINE_MS = 10_000;

/** Close code the client uses when IT declares the connection dead (server never sees it). */
const CLOSE_CLIENT_LIVENESS = 4009;

// WHY: a page still running this long after its own beforeunload did not unload — the
// navigation was cancelled, or the target was a download / external app. Short enough that
// a real drop inside the window is revived promptly, long enough to outlast a genuine
// unload, which kills the timer along with the page.
const UNLOAD_SURVIVAL_MS = 1000;

type FlushWaiter = {
  resolve: () => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
  sentTs: number;
};

// INVARIANT: flushAndWait supports MULTIPLE concurrent waiters per entity. Why: previously
// flushWaiters was Map<entityId, FlushWaiter> — a second concurrent caller overwrote the
// first, whose promise was never resolved (its 2s timer eventually rejected) → hung promise
// + leaked timer. A list resolves/rejects all waiters on the single ack / on ws-close.

export class YjsProjectProvider {
  private ws: WebSocket | null = null;
  readonly projectId: string;
  readonly userId: string;
  private reconnectController = new ReconnectController();
  private _status: WsStatus = 'connecting';
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private ackDeadlineTimer: ReturnType<typeof setTimeout> | null = null;

  private entities = new Map<string, EntityYjsState>();
  private flushWaiters = new Map<string, FlushWaiter[]>();
  private pendingUpdates = new Map<string, Uint8Array[]>();
  private flushRafId: number | null = null;
  private _observerDisposables: Array<() => void> = [];

  // ── Telemetry timing state (see collab-log.ts / perf.ts) ──────────────
  private _everConnected = false;
  private _lastCloseTs: number | null = null;
  // Visibility snapshot taken at socket close. WHY: the ambient telemetry `wasHidden`
  // resets on every emit, so by the time `recovered` fires it has lost the signal — a
  // laptop-sleep outage looks visible. Captured here, the tab is already hidden when the
  // socket dies, correctly tagging benign sleep/away outages.
  private _closeWasHidden = false;
  // true between a non-auth close and the first post-reconnect entity sync — drives the
  // `recovered` (drop→usable) metric and the `first` flag on sync-slow.
  private _pendingRecovery = false;
  // WHY: a separate flag rather than the controller's markIntentional(), which is sticky for
  // the provider's lifetime and cannot be revoked. See the guard in _onSocketClosed.
  private _unloading = false;
  private _unloadSurvivalTimer: ReturnType<typeof setTimeout> | null = null;
  private _syncStartTs = new Map<string, number>();

  constructor(projectId: string, userId: string) {
    this.projectId = projectId;
    this.userId = userId;
    this._beforeUnload = this._beforeUnload.bind(this);
    this._onForeground = this._onForeground.bind(this);
    window.addEventListener('beforeunload', this._beforeUnload);
    window.addEventListener('pageshow', this._onForeground);
    document.addEventListener('visibilitychange', this._onForeground);
  }

  get status(): WsStatus {
    return this._status;
  }

  hasEntity(entityId: string): boolean {
    return this.entities.has(entityId);
  }

  get reconnectAttemptsCount(): number {
    return this.reconnectController.attempts;
  }

  get maxReconnectAttempts(): number {
    return MAX_RECONNECT_ATTEMPTS;
  }

  connect() {
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return;
    this._setStatus(this._status === 'connected' ? 'reconnecting' : 'connecting');
    logCollabEvent('yjs', 'connect', undefined, {
      initial: !this._everConnected,
      attempt: this.reconnectController.attempts,
    });

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.ws = new WebSocket(`${proto}//${location.host}/ws/collab/project/${this.projectId}`);
    this.ws.binaryType = 'arraybuffer';

    this.ws.onopen = () => {
      this.reconnectController.resetBackoff();
      this._setStatus('connected');
      logCollabEvent('yjs', 'open', undefined, {
        outageMs: this._lastCloseTs !== null ? Date.now() - this._lastCloseTs : undefined,
      });
      this._everConnected = true;
      // A fresh socket means every entity needs a (re)join — the entity's own stored type
      // drives it. Entities still loading their local mirror are skipped here and join
      // themselves when ready (_joinAndSync); the entity map is the only join queue.
      for (const [entityId, entity] of Array.from(this.entities)) {
        this._joinAndSync(entity.entityType, entityId);
      }
      this._startHeartbeat();
    };

    this.ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        this._handleBinary(event.data);
      } else {
        try {
          const msg = JSON.parse(event.data);
          this._dispatchJson(msg);
        } catch (e) {
          // WHY: log only — one unparseable control frame is dropped; the binary sync
          // stream carries the document, and a real outage surfaces via the connection status.
          console.warn('[yjs-provider] malformed JSON', e);
        }
      }
    };

    const socket = this.ws;
    this.ws.onclose = (event: CloseEvent) => {
      // INVARIANT: the drop path runs ONCE per socket — a close event from a socket
      // that is no longer the current one is dropped here.
      // Why: _dropDeadConnection() declares a socket dead, detaches it (this.ws = null)
      // and runs the drop path itself; the browser still delivers that socket's close
      // event afterwards, and letting it through scheduled a SECOND reconnect and
      // inflated the attempt count toward gave-up.
      if (this.ws !== socket) return;
      this._onSocketClosed(event.code, event.reason, event.wasClean);
    };

    this.ws.onerror = () => {};
  }

  /**
   * The drop path — shared by the socket's own close event and by the client-side
   * liveness deadline, which has no close event to wait for.
   */
  private _onSocketClosed(code: number, reason: string, wasClean: boolean) {
    this._clearHeartbeat();
    this._flushPendingUpdates();
    this._lastCloseTs = Date.now();
    this._closeWasHidden = typeof document !== 'undefined' && document.hidden;
    // WHY: this `code` is the key previously-discarded data point. 4008 = server
    // heartbeat timeout (transient); 1006 = abnormal/network blip; 1001/1012 = backend
    // going away/restart; auth codes (4001/4003/4004) are permanent; 4009 = the client's
    // own liveness deadline (no ack, see _armAckDeadline).
    logCollabEvent('yjs', 'close', undefined, {
      code,
      reason,
      wasClean,
      intentional: this.reconnectController.intentional || this._unloading,
      isAuth: isAuthCloseCode(code),
    });
    // INVARIANT: unload suppression is REVOCABLE — a page that survives its own beforeunload
    // reconnects again. Why: latching the sticky markIntentional() here left a surviving page
    // (cancelled navigation, download link, bfcache Back) with a dead socket, no reconnect and
    // no status change, so the document rendered stale while looking connected until F5.
    if (this.reconnectController.intentional || this._unloading) return;

    // ARCH: Auth errors are permanent — no reconnect will fix them.
    // 4001=Unauthorized, 4003=No access, 4004=Not found (see ws-close-codes). 4008
    // (heartbeat timeout) is transient and intentionally NOT permanent.
    if (isAuthCloseCode(code)) {
      this._setStatus('offline');
      // INVARIANT(security): an auth-class close DELETES every mirror of THIS PROJECT, not
      // only the ones currently joined. Why: the close means this user may no longer read
      // the project, and the refused user never joins anything — a per-entity sweep here
      // runs over an empty map and leaves the text readable (found by the live drive).
      for (const entity of this.entities.values()) entity.local = null;
      void clearProjectLocalDocs(this.projectId);
      for (const entity of this.entities.values()) {
        this._broadcast(entity, cb => cb.onStatusChange('offline'));
      }
      return;
    }

    this._rejectAllFlushWaiters();
    for (const entity of this.entities.values()) {
      // On reconnect, every entity is re-joined from this map with ITS OWN stored type
      // (the join message is entity-scoped, not consumer-scoped; dispatch iterates all
      // bundles after re-sync).
      entity.synced = false;
      this._broadcast(entity, cb => cb.onStatusChange('reconnecting'));
    }
    this._setStatus('reconnecting');
    this._pendingRecovery = true;
    const result = this.reconnectController.schedule(() => this.connect());
    if (result === 'gave_up') {
      logCollabEvent('yjs', 'gave-up', undefined, { attempt: this.reconnectController.attempts });
      this._pendingRecovery = false;
      this._rejectAllFlushWaiters();
      this._setStatus('offline');
      for (const entity of this.entities.values()) {
        this._broadcast(entity, cb => cb.onStatusChange('offline'));
      }
    } else {
      logCollabEvent('yjs', 'reconnect-scheduled', undefined, {
        attempt: this.reconnectController.attempts,
        delay: this.reconnectController.lastScheduledDelay,
      });
    }
  }

  disconnect() {
    this._clearHeartbeat();
    if (this.flushRafId !== null) {
      cancelAnimationFrame(this.flushRafId);
      this.flushRafId = null;
    }
    this._flushPendingUpdates();
    this.reconnectController.markIntentional();
    window.removeEventListener('beforeunload', this._beforeUnload);
    if (this._unloadSurvivalTimer) {
      clearTimeout(this._unloadSurvivalTimer);
      this._unloadSurvivalTimer = null;
    }
    window.removeEventListener('pageshow', this._onForeground);
    document.removeEventListener('visibilitychange', this._onForeground);
    for (const [entityId, entity] of this.entities) {
      this.send({ type: 'flush', entity_id: entityId });
      entity.disposeAwareness();
      if (!this._dropMirrorIfFullySynced(entityId, entity)) void entity.local?.detach();
      entity.ydoc.destroy();
      // WHY: a torn-down entity reports synced=false to the handles still held. The
      // project page unmounts before its editor (React runs deletion cleanups
      // parent-first), so the editor's leave checkpoint reads this handle AFTER the
      // socket is gone; a stale `true` sent it into flushAndWait → a false
      // "Failed to save document" on every exit to /cabinet or /admin, although the
      // flush above had already been sent. Set after _dropMirrorIfFullySynced, which
      // reads it.
      entity.synced = false;
    }
    this._observerDisposables.forEach(d => d());
    this._observerDisposables = [];
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this._rejectAllFlushWaiters();
    this.entities.clear();
  }

  joinEntity(
    entityType: EntityType,
    entityId: string,
    callbacks: DocCollabCallbacks,
    localUser: LocalPresenceUser,
  ): EntityYjsState {
    const existing = this.entities.get(entityId);
    if (existing) {
      existing.refCount++;
      // WHY: APPEND to the bundle list — do NOT overwrite. Why: overwriting dropped
      // the first consumer's callbacks, so it silently stopped receiving onSynced /
      // onUserJoined. Every registered consumer now receives every dispatched event.
      existing.callbackBundles.push(callbacks);
      return existing;
    }

    const ydoc = new Y.Doc();
    const ytext = ydoc.getText('content');
    const local = attachLocalDoc(this.projectId, entityId, ydoc);

    const onUpdate = (update: Uint8Array, origin: unknown) => {
      // INVARIANT: updates the local mirror replayed into the doc are NOT streamed to the
      // server as incremental updates. Why: they are replayed before the join, when there is
      // no session to receive them; the whole restored state goes up as the sync step2 in
      // _syncEntity instead, which is why the join waits for the mirror to load.
      if (origin === 'remote') return;
      const current = this.entities.get(entityId);
      if (local !== null && local.isOwnOrigin(origin)) {
        // Replayed out of the mirror: state the server has never seen, and it never
        // enters the update stream. _syncEntity pushes it and clears the flag.
        if (current) current.hasUnsyncedLocalEdits = true;
        return;
      }
      // INVARIANT: an edit is remembered as unsynced exactly when the server could NOT
      // have received it — the frame never went out, or the entity is not joined yet.
      // Why: keying this on `synced` instead marks the ordinary join window — join sent,
      // step2 not back yet — where updates DO reach the server, and that pushes the whole
      // document up on a plain open, relaying it to every peer and enqueueing a handoff
      // backup checkpoint for an edit nobody made. Not-joined-yet means the mirror is
      // still loading, so no join was sent and the server would drop the update.
      const sent = this._sendUpdate(entityId, update);
      if (current && (!sent || !current.localLoaded)) current.hasUnsyncedLocalEdits = true;
    };
    ydoc.on('update', onUpdate);
    const disposeObserver = () => ydoc.off('update', onUpdate);
    this._observerDisposables.push(disposeObserver);

    // INVARIANT: awareness is per-entity, bound to that entity's Y.Doc. Why: relative
    // cursor positions are only valid against their own ydoc; sharing one Awareness
    // across entities corrupts positions on document switch.
    const awareness = new YjsAwarenessAdapter(ydoc, localUser);
    const aw = awareness.getAwareness();
    const onAwarenessUpdate = (
      changes: { added: number[]; updated: number[]; removed: number[] },
      origin: unknown,
    ) => {
      if (origin === 'remote') return;
      const changed = [...changes.added, ...changes.updated, ...changes.removed];
      if (changed.length === 0) return;
      const payload = encodeAwarenessUpdate(aw, changed);
      this._sendBinary(wrapBinary(entityId, MSG_AWARENESS, payload));
    };
    aw.on('update', onAwarenessUpdate);
    // INVARIANT: destroy() FIRST, then detach the listener. Why: destroy() broadcasts the
    // local-user awareness removal via the 'update' event; if we detach FIRST (the old
    // order), peers never receive the removal and keep a stale cursor/gutter bar for ~30s
    // (the awareness timeout) after a clean leave. Mirrors the correct order in
    // yjs-binding.ts YjsAwarenessAdapter.destroy (removeAwarenessStates before destroy).
    const disposeAwareness = () => {
      awareness.destroy();
      aw.off('update', onAwarenessUpdate);
    };

    const bundles: DocCollabCallbacks[] = [callbacks];
    const entity: EntityYjsState = {
      entityType,
      ydoc,
      ytext,
      awareness,
      callbackBundles: bundles,
      local,
      localLoaded: local === null,
      hasUnsyncedLocalEdits: false,
      synced: false,
      refCount: 1,
      // Per-registration teardown: remove THIS caller's bundle by reference so concurrent
      // consumers of the same entity are not torn down together.
      leave: () => this.leaveEntity(entityId, callbacks),
      disposeObserver,
      disposeAwareness,
      // Report the local selection to the advisory region-lock registry.
      // Bound here (not in awareness) because the backend needs absolute code-point
      // offsets in a JSON control message, not Yjs relative positions.
      reportSelection: (from, to) => {
        this.send({ type: 'selection', entity_id: entityId, from_cp: from, to_cp: to });
      },
    };
    this.entities.set(entityId, entity);

    if (local !== null) {
      local.whenReady.then(() => {
        if (this.entities.get(entityId) !== entity) return;
        entity.localLoaded = true;
        this._joinAndSync(entityType, entityId);
      });
    }
    this._joinAndSync(entityType, entityId);

    return entity;
  }

  /**
   * The single join+sync path. An entity whose local mirror is still loading is NOT
   * joined yet — its join is re-driven from the whenReady handler above.
   *
   * INVARIANT: no step1 goes out before the mirror is applied. Why: step1 carries the
   * client's state vector, so a step1 sent against a not-yet-restored doc would tell the
   * server the client has nothing, and the edits the mirror is about to restore would be
   * dropped instead of pushed (they never re-enter the update stream — see onUpdate).
   */
  private _joinAndSync(entityType: EntityType, entityId: string) {
    const entity = this.entities.get(entityId);
    if (!entity || !entity.localLoaded) return;
    if (this._status !== 'connected') return;
    this._sendJoin(entityType, entityId);
    this._syncEntity(entityId);
  }

  /**
   * Clean exit: the document ended with everything it held already on the server, so the
   * mirror has no job left and is deleted rather than kept.
   *
   * INVARIANT: only when connected AND synced AND holding no unsynced edits. Why: those
   * three are what make "the server has it" true; drop any one of them and this deletes
   * the copy in exactly the case the copy exists for. Anything this misses (a killed tab,
   * a dropped unload handler) is picked up by sweepLocalDocs.
   */
  private _dropMirrorIfFullySynced(entityId: string, entity: EntityYjsState): boolean {
    if (this._status !== 'connected' || !entity.synced || entity.hasUnsyncedLocalEdits) return false;
    this._dropLocalCopy(entityId);
    return true;
  }

  /**
   * Delete one entity's local mirror — the entity is gone or is no longer ours to hold.
   *
   * Drops the handle and deletes by key: clearLocalDoc detaches the live mirror before
   * deleting the database (see the liveMirrors invariant in local-doc-persistence), so
   * this is safe to call with the Y.Doc about to be destroyed underneath it.
   */
  private _dropLocalCopy(entityId: string): void {
    const entity = this.entities.get(entityId);
    if (entity) entity.local = null;
    void clearLocalDoc(this.projectId, entityId);
  }

  /** Dispatch a callback to EVERY registered consumer bundle of an entity
   *  (snapshot semantics — see broadcastToBundles in yjs-events). */
  private _broadcast(entity: EntityYjsState, fn: (cb: DocCollabCallbacks) => void): void {
    broadcastToBundles(entity.callbackBundles, fn);
  }

  leaveEntity(entityId: string, leavingCallbacks?: DocCollabCallbacks) {
    // WHY: Entity cleanup is intentionally split between useCollabConnection  Why: splitting join (useCollabConnection) from leave (Editor effect) keeps checkpoint-before-leave under the Editor's lifecycle, where the leaving entity is known.
    // (join) and Editor effect cleanup (leave via EntityYjsState.leave). The hook
    // manages the WS join message; the Editor manages checkpoint-before-leave
    // sequencing. This separation is correct — join needs only the WS connection,
    // while leave needs the editor state for checkpoint logic.
    this._flushEntityPending(entityId);
    const entity = this.entities.get(entityId);
    if (!entity) return;
    // Remove only THIS registration's bundle when a reference is supplied (multi-consumer
    // support: split view, agent + editor). Without a reference (legacy direct call) tear
    // down the whole entity — preserves the existing public behavior.
    if (leavingCallbacks !== undefined) {
      const idx = entity.callbackBundles.indexOf(leavingCallbacks);
      if (idx >= 0) entity.callbackBundles.splice(idx, 1);
    }
    entity.refCount--;
    if (entity.refCount > 0 && entity.callbackBundles.length > 0) return;
    this.send({ type: 'leave', entity_id: entityId });
    entity.disposeAwareness();
    entity.disposeObserver();
    // A leave with everything synced retires the mirror; otherwise it is kept, because the
    // unsynced state is exactly what it exists to carry. Either way detach BEFORE destroying
    // the doc — writing into a destroyed doc is not something the mirror survives.
    if (!this._dropMirrorIfFullySynced(entityId, entity)) void entity.local?.detach();
    entity.ydoc.destroy();
    this.entities.delete(entityId);
  }

  send(msg: Record<string, unknown>) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  flushAndWait(entityId: string, timeoutMs = 2000): Promise<void> {
    return new Promise((resolve, reject) => {
      if (this.ws?.readyState !== WebSocket.OPEN) {
        reject(new Error('ws-not-open'));
        return;
      }
      const timer = setTimeout(() => {
        // Remove only THIS waiter from the list (others may still be pending).
        const list = this.flushWaiters.get(entityId);
        if (list) {
          const i = list.indexOf(waiter);
          if (i >= 0) list.splice(i, 1);
          if (list.length === 0) this.flushWaiters.delete(entityId);
        }
        reject(new Error('flush-ack-timeout'));
      }, timeoutMs);
      const waiter: FlushWaiter = { resolve, reject, timer, sentTs: Date.now() };
      const list = this.flushWaiters.get(entityId);
      if (list) list.push(waiter);
      else this.flushWaiters.set(entityId, [waiter]);
      this.send({ type: 'flush', entity_id: entityId });
    });
  }

  private _sendJoin(entityType: EntityType, entityId: string) {
    this.send({ type: 'join', entity_type: entityType, entity_id: entityId });
  }

  // Record sync latency (time-to-usable) for an entity whose `synced` just flipped
  // false→true, and emit the reconnect `recovered` (drop→usable) metric on the first
  // sync after a non-auth close. Called at every synced-flip site.
  private _recordSynced(entityId: string): void {
    const start = this._syncStartTs.get(entityId);
    if (start !== undefined) {
      this._syncStartTs.delete(entityId);
      recordSyncLatency(entityId, Date.now() - start, { first: this._pendingRecovery });
    }
    if (this._pendingRecovery) {
      this._pendingRecovery = false;
      if (this._lastCloseTs !== null) {
        recordRecovered(entityId, Date.now() - this._lastCloseTs, { wasHidden: this._closeWasHidden });
      }
    }
  }

  private _syncEntity(entityId: string) {
    const entity = this.entities.get(entityId);
    if (!entity) return;
    this._syncStartTs.set(entityId, Date.now());
    const stateVector = Y.encodeStateVector(entity.ydoc);
    const syncMsg = new Uint8Array(2 + stateVector.length);
    syncMsg[0] = MSG_SYNC;
    syncMsg[1] = SYNC_STEP1;
    syncMsg.set(stateVector, 2);
    const wrapped = wrapBinary(entityId, MSG_SYNC, syncMsg);
    this._sendBinary(wrapped);

    // INVARIANT: edits made while unsynced push the doc's full state up right after step1,
    // and ONLY then. Why: this server never sends a step1 of its own, so step1/step2 alone
    // only ever moves state DOWN — anything the client holds and the server does not (typed
    // during an outage, or replayed from the local mirror) would never arrive. Pushing on
    // every sync instead would make a plain network blip mark the doc dirty, relay the whole
    // document to every peer, and count as this user's first push — enqueueing a handoff
    // backup checkpoint for an edit nobody made.
    if (entity.hasUnsyncedLocalEdits) {
      const localState = Y.encodeStateAsUpdate(entity.ydoc);
      const step2 = new Uint8Array(2 + localState.length);
      step2[0] = MSG_SYNC;
      step2[1] = SYNC_STEP2;
      step2.set(localState, 2);
      if (this._sendBinary(wrapBinary(entityId, MSG_SYNC, step2))) {
        entity.hasUnsyncedLocalEdits = false;
      }
    }
  }

  /** Returns whether the frame actually went out (see _sendBinary). */
  private _sendUpdate(entityId: string, update: Uint8Array): boolean {
    const syncMsg = new Uint8Array(2 + update.length);
    syncMsg[0] = MSG_SYNC;
    syncMsg[1] = SYNC_UPDATE;
    syncMsg.set(update, 2);
    const wrapped = wrapBinary(entityId, MSG_SYNC, syncMsg);
    return this._sendBinary(wrapped);
  }

  /** Returns whether the frame actually went out — a closed socket drops it silently. */
  private _sendBinary(data: Uint8Array<ArrayBuffer>): boolean {
    if (this.ws?.readyState !== WebSocket.OPEN) return false;
    this.ws.send(data);
    return true;
  }

  private _scheduleFlush(): void {
    if (this.flushRafId !== null) return;
    this.flushRafId = requestAnimationFrame(() => {
      this.flushRafId = null;
      this._flushPendingUpdates();
    });
  }

  private _applyPendingFlip(entity: EntityYjsState, entityId: string): void {
    if (!entity.synced) {
      entity.synced = true;
      this._broadcast(entity, cb => cb.onSynced());
      this._recordSynced(entityId);
    }
  }

  private _flushPendingUpdates(): void {
    for (const [entityId, updates] of this.pendingUpdates) {
      const entity = this.entities.get(entityId);
      if (!entity || updates.length === 0) continue;

      const merged = updates.length === 1 ? updates[0] : mergeUpdates(updates);
      Y.applyUpdate(entity.ydoc, merged, 'remote');
      this._applyPendingFlip(entity, entityId);
    }
    this.pendingUpdates.clear();
  }

  private _flushEntityPending(entityId: string): void {
    const updates = this.pendingUpdates.get(entityId);
    if (!updates || updates.length === 0) {
      this.pendingUpdates.delete(entityId);
      return;
    }
    const entity = this.entities.get(entityId);
    if (!entity) {
      this.pendingUpdates.delete(entityId);
      return;
    }

    const merged = updates.length === 1 ? updates[0] : mergeUpdates(updates);
    Y.applyUpdate(entity.ydoc, merged, 'remote');
    this._applyPendingFlip(entity, entityId);
    this.pendingUpdates.delete(entityId);
  }

  private _handleBinary(data: ArrayBuffer) {
    const unwrapped = unwrapBinary(data);
    if (!unwrapped) return;
    const { entityId, msgType, payload } = unwrapped;
    const entity = this.entities.get(entityId);
    if (!entity) return;

    if (msgType === MSG_SYNC && payload.length >= 2) {
      const syncType = payload[1];
      const syncPayload = payload.slice(2);

      if (syncType === SYNC_STEP2) {
        // Full sync — apply synchronously (initial load / reconnection).
        // Clear any queued incremental updates since the full state replaces them.
        this.pendingUpdates.delete(entityId);
        Y.applyUpdate(entity.ydoc, syncPayload, 'remote');
        this._applyPendingFlip(entity, entityId);
      } else if (syncType === SYNC_UPDATE) {
        // Incremental update — queue for rAF batching.
        if (!this.pendingUpdates.has(entityId)) {
          this.pendingUpdates.set(entityId, []);
        }
        this.pendingUpdates.get(entityId)!.push(syncPayload);
        this._scheduleFlush();
      } else if (syncType === SYNC_STEP1) {
        const remoteState = syncPayload;
        const diff = Y.encodeStateAsUpdate(entity.ydoc, remoteState.length > 0 ? remoteState : undefined);
        const response = new Uint8Array(2 + diff.length);
        response[0] = MSG_SYNC;
        response[1] = SYNC_STEP2;
        response.set(diff, 2);
        const wrapped = wrapBinary(entityId, MSG_SYNC, response);
        this._sendBinary(wrapped);
      }
    } else if (msgType === MSG_AWARENESS && entity.awareness) {
      entity.awareness.receiveRemote(payload);
    }
  }

  private _dispatchJson(msg: Record<string, unknown>) {
    const entityId = msg.entity_id as string | undefined;
    const entity = entityId ? this.entities.get(entityId) : undefined;
    const bundles = entity ? entity.callbackBundles : null;

    // A deleted entity and a revoked entity both end this client's claim on the text,
    // so the local mirror goes with them — before the consumers react and unmount.
    if (entityId && (msg.type === 'doc_deleted' || msg.type === 'access_revoked')) {
      this._dropLocalCopy(entityId);
    }

    // Doc-layer frames first (doc-callbacks.ts), then the entity-generic table
    // (yjs-events.ts); the remainder needs provider internals and stays here.
    if (dispatchDocControlFrame(msg, bundles)) return;
    if (dispatchEntityFrame(msg, bundles)) return;

    switch (msg.type) {
      case 'user_left':
        if (entity && isString(msg.user_id)) {
          const userId = msg.user_id;
          // Drop the leaver's gutter bar immediately, not after the ~30s awareness
          // timeout — keeps the bar in sync with the chip (removed via onUserLeft).
          removeAwarenessStatesForUser(entity.awareness.getAwareness(), userId);
          this._broadcast(entity, cb => cb.onUserLeft(userId));
        }
        break;

      case 'heartbeat_ack': {
        // The ack is also the liveness proof — it disarms the deadline (_armAckDeadline).
        this._clearAckDeadline();
        // RTT = now − the client `t` we sent with the heartbeat (windowed aggregate).
        if (typeof msg.t === 'number') recordRtt(Date.now() - msg.t);
        break;
      }

      case 'flush_ack': {
        if (isString(msg.entity_id)) {
          const list = this.flushWaiters.get(msg.entity_id);
          if (list) {
            for (const w of list) {
              clearTimeout(w.timer);
              // Edit→ack latency (the "does typing feel smooth" signal).
              recordAckLatency(msg.entity_id, Date.now() - w.sentTs);
              w.resolve();
            }
            this.flushWaiters.delete(msg.entity_id);
          }
        }
        break;
      }
    }
  }

  private _clearHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    this._clearAckDeadline();
  }

  private _startHeartbeat() {
    this._clearHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      // Carry a client timestamp so the server's heartbeat_ack lets us measure RTT.
      this.send({ type: 'heartbeat', t: Date.now() });
      this._armAckDeadline();
    }, HEARTBEAT_INTERVAL_MS);
  }

  private _clearAckDeadline() {
    if (this.ackDeadlineTimer) {
      clearTimeout(this.ackDeadlineTimer);
      this.ackDeadlineTimer = null;
    }
  }

  /**
   * INVARIANT: a heartbeat left unanswered past HEARTBEAT_ACK_DEADLINE_MS drops the
   * connection, even though the socket still reads OPEN.
   * Why: on a half-open socket (server frozen, network blackholed — no FIN or RST) the
   * browser reports readyState OPEN and send() succeeds, so collabStatus stayed
   * 'connected' and the editor kept accepting input that went nowhere — measured at 150s
   * with the backend paused. The ack is the only evidence the server is still there.
   */
  private _armAckDeadline() {
    if (this.ackDeadlineTimer) return; // one deadline in flight per socket
    this.ackDeadlineTimer = setTimeout(() => {
      this.ackDeadlineTimer = null;
      this._dropDeadConnection();
    }, HEARTBEAT_ACK_DEADLINE_MS);
  }

  /** Declare the current socket dead and run the ordinary drop path on it. */
  private _dropDeadConnection() {
    const dead = this.ws;
    if (!dead) return;
    logCollabEvent('yjs', 'ack-deadline', undefined, { deadlineMs: HEARTBEAT_ACK_DEADLINE_MS });
    // Detach before closing: the reconnect is driven from here, and a late close event
    // from this socket must not run the drop path a second time.
    this.ws = null;
    try {
      dead.close(CLOSE_CLIENT_LIVENESS, 'client liveness deadline');
    } catch {
      // A socket the browser already tore down — the drop path below is what matters.
    }
    this._onSocketClosed(CLOSE_CLIENT_LIVENESS, 'client liveness deadline', false);
  }

  private _setStatus(s: WsStatus) {
    this._status = s;
  }

  private _rejectAllFlushWaiters() {
    for (const [, list] of this.flushWaiters) {
      for (const w of list) {
        clearTimeout(w.timer);
        w.reject(new Error('ws-closed'));
      }
    }
    this.flushWaiters.clear();
  }

  private _beforeUnload() {
    for (const [entityId] of this.entities) {
      this.send({ type: 'flush', entity_id: entityId });
    }
    // WHY: mark the disconnect intentional on page unload so the socket
    // close does NOT drive the reconnect path (which fires a persistent
    // "reconnecting" toast that flashes during teardown — the F5 bug).  Why: on unload the socket closes; without marking it intentional the provider treats the close as a drop and runs the reconnect path → a 'reconnecting' toast flashing during teardown.
    // Why: React's unmount cleanup (disconnect()) does not run on hard reload,
    // so without this onclose sees intentional=false and shows the toast.
    // A page that truly unloads never runs the timer below — it is gone. One that survives
    // does, and revokes the suppression. Why: the survival is not always observable as a
    // visibility or pageshow event (a cancelled navigation and a download link both leave
    // the page visible throughout), so the foreground handler alone cannot revoke this.
    this._unloading = true;
    if (this._unloadSurvivalTimer) clearTimeout(this._unloadSurvivalTimer);
    this._unloadSurvivalTimer = setTimeout(() => {
      this._unloadSurvivalTimer = null;
      this._unloading = false;
      this._reviveIfDead();
    }, UNLOAD_SURVIVAL_MS);
  }

  /**
   * The page is in the foreground again — via bfcache restore, a cancelled navigation, or
   * a tab switch. Revokes the unload suppression and revives a socket that died while the
   * page was away.
   *
   * WHY: this covers the whole class, not just the surviving-unload latch — a tab returning
   * to the foreground with a dead socket reconnects regardless of what killed it. connect()
   * no-ops on a socket that is still CONNECTING or OPEN, so a visibility storm costs nothing.
   */
  private _onForeground() {
    if (document.visibilityState === 'hidden') return;
    this._unloading = false;
    this._reviveIfDead();
  }

  /** Reconnect now if the socket is gone. A live or connecting socket is left alone. */
  private _reviveIfDead() {
    if (this.reconnectController.intentional) return;
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return;
    this.reconnectController.cancel();
    this.reconnectController.resetBackoff();
    this.connect();
  }
}
