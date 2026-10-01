/** Project-level WS: lifecycle broadcasts + chat frames.
 *  No OT, no content sync — only receives broadcasts from the backend project WS.
 */
// ARCH: Broadcast-only WS — no OT, no content sync: document/reference lifecycle
// broadcasts plus verbatim chat frames (the chat seam's only transport here).
// ARCH: the project WS forwards an event's emitted kwargs verbatim (minus
// project_id); the field names ARE the contract, typed once in
// frontend/src/events/event-types.ts under 'ws:<type>'. dispatch has three arms
// only: init (status), chat_frame (callbacks), everything else known →
// emit(`ws:<type>`, payload) — never projected, never renamed.
// SYSTEM: project-ws — project-level WebSocket for document/reference lifecycle
// broadcasts and chat frames

import type { WsStatus } from './ws-status';
import { ReconnectController, MAX_RECONNECT_ATTEMPTS } from './reconnect-controller';
import { isAuthCloseCode } from './ws-close-codes';
import { logCollabEvent } from './collab-log';
import { emit } from '../events';
import { WS_EVENT_TYPES, type EventMap, type WsEventKey } from '../events/event-types';
import { t } from '../i18n';

const HEARTBEAT_INTERVAL_MS = 30_000;

const _KNOWN_WS_TYPES: ReadonlySet<string> = new Set(WS_EVENT_TYPES);

export interface ProjectWsCallbacks {
  // One chat_frame envelope from the owner-filtered fan-out (SYSTEM:
  // chat-fanout) — the frame is VERBATIM, and the chat store's dispatch owns
  // what it does with it (a session with no open turn is another tab's
  // business, not this connection's).
  onChatFrame: (sessionId: string, frame: Record<string, unknown>) => void;
  // Fired when a socket OPENS after
  // a drop (attempts > 0 at open). Frames pushed into the outage are gone for
  // this client (the fan-out sends to live sockets only) — the consumer
  // reloads what it must (the store's open-harness-turn resync).
  onProjectWsResync: () => void;
  onStatusChange: (status: WsStatus) => void;
  onError: (message: string) => void;
}

function isString(v: unknown): v is string { return typeof v === 'string'; }

export class ProjectConnection {
  private ws: WebSocket | null = null;
  private projectId: string;
  private callbacks: ProjectWsCallbacks;
  private reconnectController = new ReconnectController();
  private _status: WsStatus = 'connecting';
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private _everConnected = false;
  private _lastCloseTs: number | null = null;
  // Forward-compat, not a user-facing op: one warn per unknown type, ever.
  private _warnedUnknownTypes = new Set<string>();

  constructor(projectId: string, callbacks: ProjectWsCallbacks) {
    this.projectId = projectId;
    this.callbacks = callbacks;
  }

  get status(): WsStatus {
    return this._status;
  }

  connect() {
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return;
    this.setStatus(this._status === 'connected' ? 'reconnecting' : 'connecting');
    logCollabEvent('project', 'connect', undefined, {
      initial: !this._everConnected,
      attempt: this.reconnectController.attempts,
    });

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.ws = new WebSocket(`${proto}//${location.host}/ws/project/${this.projectId}`);

    this.ws.onopen = () => {
      // Read BEFORE resetBackoff zeroes it: attempts > 0 means this open
      // followed a drop — the resync signal (frames were lost in the gap).
      const reopenedAfterDrop = this.reconnectController.attempts > 0;
      this.reconnectController.resetBackoff();
      logCollabEvent('project', 'open', undefined, {
        outageMs: this._lastCloseTs !== null ? Date.now() - this._lastCloseTs : undefined,
      });
      this._everConnected = true;
      this._startHeartbeat();
      if (reopenedAfterDrop) this.callbacks.onProjectWsResync();
    };

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        this.dispatch(msg);
      } catch (e) {
        console.warn('[project-ws] malformed WS message', e);
        this.callbacks.onError('Malformed server message');
      }
    };

    this.ws.onclose = (event: CloseEvent) => {
      this._clearHeartbeat();
      this._lastCloseTs = Date.now();
      logCollabEvent('project', 'close', undefined, {
        code: event.code,
        reason: event.reason,
        wasClean: event.wasClean,
        intentional: this.reconnectController.intentional,
        isAuth: isAuthCloseCode(event.code),
      });
      if (this.reconnectController.intentional) return;
      // ARCH: auth errors are permanent (4001=Unauthorized, 4003=No access, 4004=Not
      // found) — no reconnect can fix them. Previously project-ws reconnected up to
      // MAX_RECONNECT_ATTEMPTS (~10+ min) after a 4003 kick, wasteful and confusing
      // (reconnecting toast after being kicked). Mirrors yjs-provider's policy via the
      // shared AUTH_CLOSE_CODES constant so the two clients never drift.
      if (isAuthCloseCode(event.code)) {
        this.setStatus('offline');
        this.callbacks.onError(t('accessRevokedOrExpired'));
        return;
      }
      this.setStatus('reconnecting');
      const result = this.reconnectController.schedule(() => this.connect());
      if (result === 'gave_up') {
        console.warn(`[project-ws] giving up after ${MAX_RECONNECT_ATTEMPTS} attempts`);
        logCollabEvent('project', 'gave-up', undefined, { attempt: this.reconnectController.attempts });
        this.setStatus('offline');
      } else {
        logCollabEvent('project', 'reconnect-scheduled', undefined, {
          attempt: this.reconnectController.attempts,
          delay: this.reconnectController.lastScheduledDelay,
        });
      }
    };

    this.ws.onerror = (event) => {
      console.warn('[project-ws] error', event);
    };
  }

  disconnect() {
    this._clearHeartbeat();
    this.reconnectController.markIntentional();
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  private setStatus(s: WsStatus) {
    this._status = s;
    this.callbacks.onStatusChange(s);
  }

  private dispatch(msg: Record<string, unknown>) {
    const type = msg.type;
    if (type === 'init') {
      this.setStatus('connected');
      return;
    }
    if (type === 'chat_frame') {
      // Beside the image chips (the fan-out's own event table row): the
      // harness lifecycle's chat frames ride this channel owner-filtered.
      const frame = msg.frame;
      if (!isString(msg.session_id) || !frame || typeof frame !== 'object') return;
      this.callbacks.onChatFrame(msg.session_id, frame as Record<string, unknown>);
      return;
    }
    if (typeof type === 'string' && _KNOWN_WS_TYPES.has(type)) {
      const payload = { ...msg };
      delete payload.type;
      emit(`ws:${type}` as WsEventKey, payload as EventMap[WsEventKey]);
      return;
    }
    // Unknown type: the backend grew a new event this build does not know.
    // Forward-compat, not a user-facing op — warn once per type and move on.
    if (typeof type === 'string' && !this._warnedUnknownTypes.has(type)) {
      this._warnedUnknownTypes.add(type);
      console.warn(`[project-ws] unknown message type "${type}" — not in WS_EVENT_TYPES`);
    }
  }

  private _clearHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private _startHeartbeat() {
    this._clearHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'heartbeat' }));
      }
    }, HEARTBEAT_INTERVAL_MS);
  }

}
