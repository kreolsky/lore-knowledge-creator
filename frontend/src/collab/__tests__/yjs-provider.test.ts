/** Tests for YjsProjectProvider — join/leave lifecycle, binary sync, close codes, heartbeat, flushAndWait. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import * as Y from 'yjs';
import type { DocCollabCallbacks } from '../doc-callbacks';
import { YjsProjectProvider } from '../yjs-provider';

class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: MockWebSocket[] = [];
  static OriginalWebSocket: typeof WebSocket | null = null;

  readyState = 1;
  url = '';
  binaryType = '';
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: ArrayBuffer | string }) => void) | null = null;
  onclose: ((e: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: (string | ArrayBufferLike)[] = [];

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  send(data: string | ArrayBufferLike) { this.sent.push(data); }
  close() { this.readyState = 3; }

  simulateOpen() {
    this.readyState = 1;
    this.onopen?.();
  }
  simulateMessage(msg: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  simulateBinary(data: Uint8Array) {
    this.onmessage?.({ data: data.buffer as ArrayBuffer });
  }
  simulateClose(code = 1000, reason = '') {
    this.readyState = 3;
    this.onclose?.({ code, reason });
  }

  static reset() { MockWebSocket.instances = []; }
  static latest() { return MockWebSocket.instances[MockWebSocket.instances.length - 1]; }

  static install() {
    MockWebSocket.OriginalWebSocket = globalThis.WebSocket;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).WebSocket = MockWebSocket;
  }
  static restore() {
    if (MockWebSocket.OriginalWebSocket) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (globalThis as any).WebSocket = MockWebSocket.OriginalWebSocket;
    }
  }
}

function makeCallbacks(): DocCollabCallbacks {
  return {
    onPresenceUsers: vi.fn(),
    onUserJoined: vi.fn(),
    onUserLeft: vi.fn(),
    onDocDeleted: vi.fn(),
    onAccessChanged: vi.fn(),
    onAccessRevoked: vi.fn(),
    onStatusChange: vi.fn(),
    onSynced: vi.fn(),
    onError: vi.fn(),
    onBacklinksChanged: vi.fn(),
    onCheckpointCreated: vi.fn(),
    onDocumentHistoryAdded: vi.fn(),
    onSaveDegraded: vi.fn(),
    onSaveRecovered: vi.fn(),
  };
}

const localUser = { id: 'user1', name: 'User One', color: '#2563eb' };

describe('YjsProjectProvider', () => {
  beforeEach(() => {
    MockWebSocket.reset();
    MockWebSocket.install();
    vi.useFakeTimers();
  });

  afterEach(() => {
    MockWebSocket.restore();
    vi.useRealTimers();
  });

  describe('join/leave lifecycle (refCounting)', () => {
    it('creates entity on first join, increments refCount on rejoin', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const cb = makeCallbacks();
      const entity = provider.joinEntity('doc', 'e1', cb, localUser);
      expect(entity.refCount).toBe(1);
      expect(entity.ydoc).toBeInstanceOf(Y.Doc);

      const entity2 = provider.joinEntity('doc', 'e1', cb, localUser);
      expect(entity2).toBe(entity);
      expect(entity.refCount).toBe(2);

      provider.leaveEntity('e1');
      expect(entity.refCount).toBe(1);
      expect(provider.hasEntity('e1')).toBe(true);

      provider.leaveEntity('e1');
      expect(provider.hasEntity('e1')).toBe(false);

      provider.disconnect();
    });

    it('disposes observer on leaveEntity', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const cb = makeCallbacks();
      const entity = provider.joinEntity('doc', 'e1', cb, localUser);

      const updateSpy = vi.fn();
      entity.ydoc.on('update', updateSpy);

      provider.leaveEntity('e1');
      expect(provider.hasEntity('e1')).toBe(false);

      Y.applyUpdate(entity.ydoc, Y.encodeStateAsUpdate(entity.ydoc));
      expect(updateSpy).not.toHaveBeenCalled();

      provider.disconnect();
    });
  });

  describe('binary sync message handling (wrap/unwrap)', () => {
    it('sends SYNC_STEP1 on connect for existing entities', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const cb = makeCallbacks();
      provider.joinEntity('doc', 'e1', cb, localUser);

      const joinMsg = ws.sent.filter(s => typeof s === 'string').map(s => JSON.parse(s as string));
      expect(joinMsg.some(m => m.type === 'join' && m.entity_id === 'e1')).toBe(true);

      const binaryMsgs = ws.sent.filter(s => s instanceof ArrayBuffer || s instanceof Uint8Array);
      expect(binaryMsgs.length).toBeGreaterThan(0);

      provider.disconnect();
    });
  });

  describe('close frame parsing (auth codes → offline)', () => {
    it('4001 close code sets status to offline (no reconnect)', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const cb = makeCallbacks();
      provider.joinEntity('doc', 'e1', cb, localUser);

      ws.simulateClose(4001, 'Unauthorized');

      expect(cb.onStatusChange).toHaveBeenCalledWith('offline');
      expect(provider.status).toBe('offline');

      provider.disconnect();
    });

    it('normal close triggers reconnect', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const cb = makeCallbacks();
      provider.joinEntity('doc', 'e1', cb, localUser);

      ws.simulateClose(1000);

      expect(cb.onStatusChange).toHaveBeenCalledWith('reconnecting');
      expect(provider.status).toBe('reconnecting');

      provider.disconnect();
    });
  });

  describe('reconnect flow', () => {
    it('reconnects after normal close', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const cb = makeCallbacks();
      provider.joinEntity('doc', 'e1', cb, localUser);
      ws.sent.length = 0;

      ws.simulateClose(1000);

      vi.advanceTimersByTime(2000);

      expect(MockWebSocket.instances.length).toBeGreaterThanOrEqual(2);

      provider.disconnect();
    });
  });

  describe('heartbeat', () => {
    it('sends heartbeat at interval while the server keeps acking', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const beats = () => ws.sent
        .filter(s => typeof s === 'string')
        .map(s => JSON.parse(s as string))
        .filter(m => m.type === 'heartbeat');

      // The ack is what keeps the beat going: an unanswered heartbeat drops the
      // connection at HEARTBEAT_ACK_DEADLINE_MS, so a silent server has no second beat
      // (see yjs-provider-liveness.test.ts — that is the contract, not a regression).
      vi.advanceTimersByTime(30_000);
      expect(beats().length).toBe(1);
      ws.simulateMessage({ type: 'heartbeat_ack', t: Date.now() });

      vi.advanceTimersByTime(30_000);
      expect(beats().length).toBe(2);

      provider.disconnect();
    });
  });

  describe('flushAndWait', () => {
    it('resolves on flush_ack', async () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const cb = makeCallbacks();
      provider.joinEntity('doc', 'e1', cb, localUser);

      const flushPromise = provider.flushAndWait('e1', 5000);

      const flushMsgs = ws.sent
        .filter(s => typeof s === 'string')
        .map(s => JSON.parse(s as string))
        .filter(m => m.type === 'flush');
      expect(flushMsgs.length).toBe(1);

      ws.simulateMessage({ type: 'flush_ack', entity_id: 'e1' });

      await expect(flushPromise).resolves.toBeUndefined();

      provider.disconnect();
    });

    it('rejects on timeout', async () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const cb = makeCallbacks();
      provider.joinEntity('doc', 'e1', cb, localUser);

      const flushPromise = provider.flushAndWait('e1', 100);

      vi.advanceTimersByTime(200);

      await expect(flushPromise).rejects.toThrow('flush-ack-timeout');

      provider.disconnect();
    });
  });

  // ── FIX 7: multi-consumer callbacks + concurrent waiters ──────────────────

  describe('multi-consumer callbacks (FIX 7)', () => {
    it('two consumers of the same entity both receive onSynced', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const cb1 = makeCallbacks();
      const cb2 = makeCallbacks();
      provider.joinEntity('doc', 'shared', cb1, localUser);
      provider.joinEntity('doc', 'shared', cb2, localUser);

      // Simulate a SYNC_STEP2 (full sync) → triggers onSynced on both bundles.
      const entity = ws.sent.filter(s => typeof s === 'string').map(s => JSON.parse(s as string));
      void entity;

      // Build a minimal SYNC_STEP2 binary frame for entity 'shared'.
      const ydoc = new Y.Doc();
      const snapshot = Y.encodeStateAsUpdate(ydoc);
      const syncMsg = new Uint8Array(2 + snapshot.length);
      syncMsg[0] = 0; // MSG_SYNC
      syncMsg[1] = 1; // SYNC_STEP2
      syncMsg.set(snapshot, 2);
      const eidBytes = new TextEncoder().encode('shared');
      const wrapped = new Uint8Array(3 + eidBytes.length + syncMsg.length);
      wrapped[0] = 0; // MSG_SYNC
      wrapped[1] = (eidBytes.length >> 8) & 0xff;
      wrapped[2] = eidBytes.length & 0xff;
      wrapped.set(eidBytes, 3);
      wrapped.set(syncMsg, 3 + eidBytes.length);
      ws.simulateBinary(wrapped);

      expect(cb1.onSynced).toHaveBeenCalledTimes(1);
      expect(cb2.onSynced).toHaveBeenCalledTimes(1);

      provider.disconnect();
    });

    it('a handle held past disconnect() no longer reports synced', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();
      const handle = provider.joinEntity('doc', 'held', makeCallbacks(), localUser);

      const snapshot = Y.encodeStateAsUpdate(new Y.Doc());
      const syncMsg = new Uint8Array(2 + snapshot.length);
      syncMsg[0] = 0; // MSG_SYNC
      syncMsg[1] = 1; // SYNC_STEP2
      syncMsg.set(snapshot, 2);
      const eidBytes = new TextEncoder().encode('held');
      const wrapped = new Uint8Array(3 + eidBytes.length + syncMsg.length);
      wrapped[1] = (eidBytes.length >> 8) & 0xff;
      wrapped[2] = eidBytes.length & 0xff;
      wrapped.set(eidBytes, 3);
      wrapped.set(syncMsg, 3 + eidBytes.length);
      ws.simulateBinary(wrapped);
      expect(handle.synced).toBe(true);

      // The editor's unmount checkpoint reads this flag after the project page
      // (its parent) has already torn the provider down.
      provider.disconnect();
      expect(handle.synced).toBe(false);
    });

    it('leaveEntity(token) of one consumer keeps the other receiving events', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const cb1 = makeCallbacks();
      const cb2 = makeCallbacks();
      const e1 = provider.joinEntity('doc', 'e2', cb1, localUser);
      provider.joinEntity('doc', 'e2', cb2, localUser);

      expect(provider.hasEntity('e2')).toBe(true);

      // Leave the first consumer only (per-registration teardown via the leave closure).
      e1.leave();

      expect(provider.hasEntity('e2')).toBe(true);

      // Remaining consumer (cb2) still receives a dispatched event.
      ws.simulateMessage({ type: 'doc_deleted', entity_id: 'e2' });
      expect(cb2.onDocDeleted).toHaveBeenCalledTimes(1);
      expect(cb1.onDocDeleted).not.toHaveBeenCalled();

      provider.disconnect();
    });
  });

  describe('concurrent flushAndWait (FIX 7)', () => {
    it('two concurrent waiters for the same entity both resolve on one ack', async () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const cb = makeCallbacks();
      provider.joinEntity('doc', 'e3', cb, localUser);

      const p1 = provider.flushAndWait('e3', 5000);
      const p2 = provider.flushAndWait('e3', 5000);

      ws.simulateMessage({ type: 'flush_ack', entity_id: 'e3' });

      await expect(p1).resolves.toBeUndefined();
      await expect(p2).resolves.toBeUndefined();

      provider.disconnect();
    });

    it('no leaked waiter entry after both resolve (flushWaiters cleared)', async () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      provider.joinEntity('doc', 'e4', makeCallbacks(), localUser);
      const p1 = provider.flushAndWait('e4', 5000);
      const p2 = provider.flushAndWait('e4', 5000);

      ws.simulateMessage({ type: 'flush_ack', entity_id: 'e4' });
      await Promise.all([p1, p2]);

      // Access private map via cast for the leak assertion.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const waiters = (provider as any).flushWaiters as Map<string, unknown>;
      expect(waiters.has('e4')).toBe(false);

      provider.disconnect();
    });
  });

  // ── FIX 8: awareness removal broadcast on graceful leave ──────────────────

  describe('awareness destroy order (FIX 8)', () => {
    it('broadcasts local awareness removal BEFORE detaching the listener', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const cb = makeCallbacks();
      const entity = provider.joinEntity('doc', 'e5', cb, localUser);
      ws.sent.length = 0; // drop join/sync frames

      entity.disposeAwareness();

      // A MSG_AWARENESS (=1) binary frame must have been sent carrying the removal.
      const awarenessFrames = ws.sent
        .filter(s => s instanceof Uint8Array)
        .map(b => new Uint8Array(b));
      // Frame layout: [msgType(1 byte=awareness), eidLenHi, eidLenLo, ...eid, payload]
      const isAwareness = awarenessFrames.some(buf => buf.length >= 3 && buf[0] === 1);
      expect(isAwareness).toBe(true);

      provider.disconnect();
    });
  });

  // ── Entity-type single point: stored per entity, rejoin reads it ───────────

  describe('entity type storage + rejoin', () => {
    it('EntityYjsState carries the entityType it was joined with', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      const entity = provider.joinEntity('doc', 'e1', makeCallbacks(), localUser);
      expect(entity.entityType).toBe('doc');

      provider.disconnect();
    });

    it('rejoin after reconnect sends entity_type from the STORED type, not a literal', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      provider.joinEntity('doc', 'e1', makeCallbacks(), localUser);
      ws.sent.length = 0;

      ws.simulateClose(1000);
      vi.advanceTimersByTime(2000);

      const ws2 = MockWebSocket.latest()!;
      ws2.simulateOpen();

      const joins = ws2.sent
        .filter(s => typeof s === 'string')
        .map(s => JSON.parse(s as string))
        .filter(m => m.type === 'join');
      expect(joins).toHaveLength(1);
      expect(joins[0].entity_type).toBe('doc');
      expect(joins[0].entity_id).toBe('e1');

      provider.disconnect();
    });
  });

  // A `beforeunload` that does NOT unload the page (cancelled navigation, a download or
  // external-app link, bfcache restore via Back) must not permanently disable reconnects.
  describe('surviving beforeunload', () => {
    function setVisibility(state: 'visible' | 'hidden') {
      Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
      Object.defineProperty(document, 'hidden', { value: state === 'hidden', configurable: true });
    }

    afterEach(() => setVisibility('visible'));

    it('still reconnects when the page survives the unload', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();
      const before = MockWebSocket.instances.length;

      window.dispatchEvent(new Event('beforeunload'));
      ws.simulateClose(1006, '');
      vi.advanceTimersByTime(2000);

      expect(MockWebSocket.instances.length).toBeGreaterThan(before);
      provider.disconnect();
    });

    it('disconnect() teardown still suppresses reconnect', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      provider.disconnect();
      const after = MockWebSocket.instances.length;
      ws.simulateClose(1006, '');
      vi.advanceTimersByTime(30_000);

      expect(MockWebSocket.instances.length).toBe(after);
    });

    it('returning to the foreground with a dead socket reconnects', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      const ws = MockWebSocket.latest()!;
      ws.simulateOpen();

      window.dispatchEvent(new Event('beforeunload'));
      ws.simulateClose(1006, '');
      const after = MockWebSocket.instances.length;

      setVisibility('visible');
      document.dispatchEvent(new Event('visibilitychange'));

      expect(MockWebSocket.instances.length).toBeGreaterThan(after);
      provider.disconnect();
    });

    it('returning to the foreground with a live socket opens no second socket', () => {
      const provider = new YjsProjectProvider('proj1', 'user1');
      provider.connect();
      MockWebSocket.latest()!.simulateOpen();
      const after = MockWebSocket.instances.length;

      setVisibility('visible');
      document.dispatchEvent(new Event('visibilitychange'));

      expect(MockWebSocket.instances.length).toBe(after);
      provider.disconnect();
    });
  });
});
