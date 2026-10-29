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
});
