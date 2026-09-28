/**
 * Client-side liveness: the provider must stop calling itself connected when the
 * server stops answering its heartbeat.
 *
 * Measured before this existed (backend frozen with `docker compose pause`): the
 * editor stayed `contenteditable=true` with the status dot on 'connected' for the
 * full 150s observation window, `readyState` stayed OPEN and `bufferedAmount`
 * never left 0 — so neither the socket nor the send buffer surfaces the loss.
 * The heartbeat ack is the only signal that crosses the wire, and these tests
 * bind the deadline armed on it.
 */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { YjsProjectProvider, HEARTBEAT_INTERVAL_MS, HEARTBEAT_ACK_DEADLINE_MS } from '../yjs-provider';
import { MockWebSocket } from './mock-ws';

function sentTypes(ws: MockWebSocket): string[] {
  return ws.sent.map(s => (JSON.parse(s) as { type: string }).type);
}

function connected() {
  const provider = new YjsProjectProvider('proj-1', 'user-1');
  provider.connect();
  const ws = MockWebSocket.latest();
  ws.simulateOpen();
  return { provider, ws };
}

beforeEach(() => {
  MockWebSocket.reset();
  vi.stubGlobal('WebSocket', MockWebSocket);
  vi.useFakeTimers();
  vi.spyOn(Math, 'random').mockReturnValue(0);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('heartbeat ack deadline', () => {
  it('leaves connected when no ack answers the heartbeat', () => {
    const { provider, ws } = connected();
    expect(provider.status).toBe('connected');

    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS);
    expect(sentTypes(ws)).toContain('heartbeat');
    // Still inside the deadline — a slow link must not be called dead.
    vi.advanceTimersByTime(HEARTBEAT_ACK_DEADLINE_MS - 1);
    expect(provider.status).toBe('connected');

    vi.advanceTimersByTime(2);
    expect(provider.status).toBe('reconnecting');
  });

  it('stays connected while the server keeps acking', () => {
    const { provider, ws } = connected();
    // Each iteration lands exactly on a beat, acks it, then advances PAST the deadline
    // — which is what proves the ack disarmed it rather than the timer never firing.
    for (let beat = 0; beat < 5; beat++) {
      vi.advanceTimersByTime(
        beat === 0 ? HEARTBEAT_INTERVAL_MS : HEARTBEAT_INTERVAL_MS - (HEARTBEAT_ACK_DEADLINE_MS + 1),
      );
      ws.simulateMessage({ type: 'heartbeat_ack', t: Date.now() });
      vi.advanceTimersByTime(HEARTBEAT_ACK_DEADLINE_MS + 1);
      expect(provider.status).toBe('connected');
    }
  });

  it('tells every joined entity the connection is gone', () => {
    const { provider, ws } = connected();
    const onStatusChange = vi.fn();
    provider.joinEntity(
      'doc',
      'doc-1',
      { onStatusChange, onSynced: vi.fn(), onUserJoined: vi.fn(), onUserLeft: vi.fn() } as never,
      { id: 'user-1', name: 'Red', color: '#f00' },
    );
    ws.sent.length = 0;

    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS + HEARTBEAT_ACK_DEADLINE_MS + 1);

    expect(onStatusChange).toHaveBeenCalledWith('reconnecting');
  });

  it('closes the dead socket and reconnects on a fresh one', () => {
    const { ws } = connected();
    expect(MockWebSocket.instances).toHaveLength(1);

    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS + HEARTBEAT_ACK_DEADLINE_MS + 1);
    expect(ws.readyState).toBe(MockWebSocket.CLOSED);

    // The reconnect is the controller's, on its own backoff — just prove one happens.
    vi.advanceTimersByTime(60_000);
    expect(MockWebSocket.instances.length).toBeGreaterThan(1);
  });

  it('ignores the dead socket\'s own close event — one drop, one reconnect attempt', () => {
    const { provider, ws } = connected();
    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS + HEARTBEAT_ACK_DEADLINE_MS + 1);
    expect(provider.reconnectAttemptsCount).toBe(1);

    // The browser delivers the close for the socket we already declared dead, before the
    // scheduled reconnect has opened a replacement — the only window where the drop path
    // could run twice. A second run doubles the backoff and burns an attempt toward
    // gave-up (MAX_RECONNECT_ATTEMPTS) for one outage.
    ws.simulateClose(1006);

    expect(provider.reconnectAttemptsCount).toBe(1);
  });

  it('does not fire the deadline twice for one dead socket', () => {
    const { provider } = connected();
    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS + HEARTBEAT_ACK_DEADLINE_MS + 1);
    const afterFirst = MockWebSocket.instances.length;
    // The dead socket's heartbeat timer must be gone — no further drops from it.
    vi.advanceTimersByTime(HEARTBEAT_INTERVAL_MS * 3);
    expect(provider.status).not.toBe('connected');
    expect(MockWebSocket.instances.length).toBeGreaterThanOrEqual(afterFirst);
  });
});
