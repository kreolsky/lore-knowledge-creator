/**
 * usePublicDocSocket — the public share's liveness channel (see SYSTEM:
 * public-share, backend routes/public_ws.py).
 *
 * One anonymous change-ping WebSocket per viewed document: the server sends
 * {"type":"changed"} the moment a note or the text of THAT document changes,
 * and this hook answers with the SAME refetch the old 5 s poll ran (load, then
 * refreshText when the list was resolved against newer text). No timer: an
 * idle tab costs one heartbeat per 30 s.
 *
 * ARCH: a per-document socket, deliberately NOT a multiplexed one (unlike the
 * member collab socket). Why: the guest already pays a full document GET per
 * in-subtree click, so a WS handshake beside it is noise, and a per-document
 * socket keeps the server's access re-proof per document.
 *
 * Coalescing is the client's (the server sends every flush): at most one
 * refetch in flight and ONE trailing run queued. A hidden tab defers — a ping
 * while hidden only sets a dirty flag; one visibilitychange listener runs a
 * single refetch on return, only if dirty. Without it a member typing would
 * make every background guest tab GET per flush — more than the old poll.
 *
 * Catch-up on (re)open and on every non-intentional, non-permanent close:
 * events during the gap are not replayed, and a network that refuses
 * WebSockets (or a guest held off by the 4029 cap) still refreshes at the
 * reconnect rate instead of freezing until give-up. A permanent close
 * (4001/4003/4004 — isAuthCloseCode) runs one last refetch (the REST 404 path
 * already marks the list stale) and never reconnects; give-up sets the store's
 * liveLost flag, which the notes panel renders as a banner.
 *
 * Store access (`usePublicNotesStore.getState()`, `publicDocIsBehind`) happens
 * ONLY inside effects/callbacks, never at render time — PublicSharePage.test
 * mocks the store module with a minimal surface.
 */
import { useEffect } from 'react';
import { ReconnectController } from '../collab/reconnect-controller';
import { isAuthCloseCode } from '../collab/ws-close-codes';
import { publicDocIsBehind, usePublicNotesStore } from '../store/public-notes-store';

const HEARTBEAT_INTERVAL_MS = 30_000;

export class PublicDocSocket {
  private ws: WebSocket | null = null;
  private documentId: string;
  private onLost: () => void;
  private reconnect = new ReconnectController();
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private running = false;
  // One trailing refetch queued while a run is in flight.
  private trailing = false;
  // A ping (or catch-up) seen while the tab was hidden.
  private deferred = false;
  private firstOpen = true;

  constructor(documentId: string, onLost: () => void) {
    this.documentId = documentId;
    this.onLost = onLost;
  }

  connect() {
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.ws = new WebSocket(`${proto}//${location.host}/ws/public/doc/${this.documentId}`);

    this.ws.onopen = () => {
      // A healthy open resets the backoff: this socket outlives its reconnects
      // (one ReconnectController for the whole lifetime, unlike
      // ProjectConnection's fresh-connection-per-open shape), so without the
      // reset each drop's attempts accumulate and a long-lived tab gives up
      // early on transient drops.
      this.reconnect.resetBackoff();
      this.startHeartbeat();
      // Catch-up on every (re)open after the first: frames pushed during the
      // outage are gone for this client.
      const reopened = !this.firstOpen;
      this.firstOpen = false;
      if (reopened) this.refetch();
    };

    this.ws.onmessage = (event: MessageEvent) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg?.type === 'changed') this.refetch();
      } catch {
        // A malformed frame refreshes nothing; the next ping re-triggers.
      }
    };

    this.ws.onclose = (event: CloseEvent) => {
      this.clearHeartbeat();
      if (this.reconnect.intentional) return;
      // Permanent (auth/lifecycle) closes cannot be reconnected out of: one
      // last refetch — the REST 404 path marks the list stale via
      // refreshFailed — and stop.
      if (isAuthCloseCode(event.code)) {
        this.refetch();
        return;
      }
      // Transient drop: catch up now, reconnect on the shared backoff.
      this.refetch();
      const result = this.reconnect.schedule(() => this.connect());
      if (result === 'gave_up') this.onLost();
    };

    this.ws.onerror = () => {
      // onclose follows every failed open; nothing to do here.
    };
  }

  disconnect() {
    this.clearHeartbeat();
    this.reconnect.markIntentional();
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  /** The old poll's refetch, coalesced: one in flight, one trailing, hidden defers. */
  refetch() {
    if (document.visibilityState !== 'visible') {
      this.deferred = true;
      return;
    }
    if (this.running) {
      this.trailing = true;
      return;
    }
    this.running = true;
    void (async () => {
      try {
        const docId = usePublicNotesStore.getState().documentId;
        if (docId) {
          await usePublicNotesStore.getState().load(docId);
          // The list came back resolved against newer text (a member edited):
          // re-read the text in place so the highlights land on the right words.
          if (publicDocIsBehind()) await usePublicNotesStore.getState().refreshText(docId);
        }
      } finally {
        this.running = false;
        if (this.trailing) {
          this.trailing = false;
          this.refetch();
        }
      }
    })();
  }

  /** One visibilitychange → visible: run the deferred refetch, if any. */
  onVisible() {
    if (this.deferred) {
      this.deferred = false;
      this.refetch();
    }
  }

  private startHeartbeat() {
    this.clearHeartbeat();
    this.heartbeat = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'heartbeat' }));
      }
    }, HEARTBEAT_INTERVAL_MS);
  }

  private clearHeartbeat() {
    if (this.heartbeat) {
      clearInterval(this.heartbeat);
      this.heartbeat = null;
    }
  }
}

/**
 * Open the change-ping socket for `documentId`. A no-op while `enabled` is
 * false (comments off — the channel exists for notes, and the server refuses
 * with 4003 anyway). Re-runs on a documentId change: a close + open per
 * in-subtree click, and never before the first list (allowComments turns true
 * only when the first load() returns). StrictMode-safe: the re-entry guard in
 * connect() plus the intentional disconnect in cleanup leave exactly one open
 * socket after a double mount.
 */
export function usePublicDocSocket(documentId: string, enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return;
    const sock = new PublicDocSocket(documentId, () => {
      usePublicNotesStore.getState().setLiveLost(true);
    });
    // A fresh connection is a fresh chance: clear the give-up banner state.
    usePublicNotesStore.getState().setLiveLost(false);
    sock.connect();
    const onVisibility = () => {
      if (document.visibilityState === 'visible') sock.onVisible();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      sock.disconnect();
    };
  }, [documentId, enabled]);
}
