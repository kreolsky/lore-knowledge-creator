/** Unit tests for ProjectConnection — mock WebSocket, test lifecycle + dispatch.
 *
 * Dispatch contract: every known message type is
 * re-emitted on the app EventBus as `ws:<type>` with the message's fields
 * VERBATIM (deep-equal minus `type`) — never projected, never renamed.
 */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ProjectConnection, type ProjectWsCallbacks } from './project-connection';
import { MockWebSocket } from './__tests__/mock-ws';
import { on, off, type EventMap } from '../events';

// ── Helpers ──────────────────────────────────────────────────────────────────

function makeCallbacks(overrides: Partial<ProjectWsCallbacks> = {}): ProjectWsCallbacks {
  return {
    onChatFrame: vi.fn(),
    onProjectWsResync: vi.fn(),
    onStatusChange: vi.fn(),
    onError: vi.fn(),
    ...overrides,
  };
}

function createConnected(overrides: Partial<ProjectWsCallbacks> = {}) {
  const cb = makeCallbacks(overrides);
  const conn = new ProjectConnection('proj-1', cb);
  conn.connect();
  const ws = MockWebSocket.latest();
  ws.simulateOpen();
  ws.simulateMessage({ type: 'init' });
  return { conn, ws, cb };
}

/** Subscribe a loose spy on the bus; returns [spy, unsub]. */
function busSpy<K extends keyof EventMap>(key: K): [ReturnType<typeof vi.fn>, () => void] {
  const spy = vi.fn();
  const handler = spy as unknown as (payload: EventMap[K]) => void;
  on(key, handler);
  return [spy, () => off(key, handler)];
}

// ── Setup ────────────────────────────────────────────────────────────────────

beforeEach(() => {
  MockWebSocket.reset();
  vi.stubGlobal('WebSocket', MockWebSocket);
  vi.useFakeTimers();
  // Reconnect delay carries up to 500ms jitter (anti-thundering-herd). Pin
  // Math.random to 0 so the exact-timing backoff assertions stay deterministic.
  vi.spyOn(Math, 'random').mockReturnValue(0);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

// ── Tests ────────────────────────────────────────────────────────────────────

describe('ProjectConnection lifecycle', () => {
  it('does not create WS in constructor', () => {
    const conn = new ProjectConnection('proj-1', makeCallbacks());
    expect(MockWebSocket.instances).toHaveLength(0);
    conn.disconnect();
  });

  it('creates WS with correct URL on connect()', () => {
    const conn = new ProjectConnection('proj-1', makeCallbacks());
    conn.connect();
    const ws = MockWebSocket.latest();
    expect(ws.url).toContain('/ws/project/proj-1');
    conn.disconnect();
  });

  it('disconnect() closes WS and prevents reconnect', () => {
    const { conn, ws } = createConnected();
    conn.disconnect();
    ws.simulateClose();
    vi.advanceTimersByTime(60_000);
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('does not create duplicate WS if already connected', () => {
    const conn = new ProjectConnection('proj-1', makeCallbacks());
    conn.connect();
    const ws = MockWebSocket.latest();
    ws.simulateOpen();
    conn.connect(); // second call — should be no-op
    expect(MockWebSocket.instances).toHaveLength(1);
    conn.disconnect();
  });
});

describe('ProjectConnection reconnection', () => {
  it('schedules reconnect on unintentional close', () => {
    const { cb, ws, conn } = createConnected();
    ws.simulateClose();
    expect(cb.onStatusChange).toHaveBeenCalledWith('reconnecting');
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(2);
    conn.disconnect();
  });

  // ── FIX 9: honor auth close codes (4001/4003/4004) ────────────────────────

  it('4003 auth close sets offline and does NOT reconnect', () => {
    const onError = vi.fn();
    const { ws, conn } = createConnected({ onError });
    ws.simulateClose(4003, 'No access');
    expect(MockWebSocket.instances).toHaveLength(1); // no reconnect scheduled
    vi.advanceTimersByTime(60_000);
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(onError).toHaveBeenCalled();
    conn.disconnect();
  });

  it('4001 auth close sets offline and does NOT reconnect', () => {
    const { ws, conn } = createConnected();
    ws.simulateClose(4001, 'Unauthorized');
    vi.advanceTimersByTime(60_000);
    expect(MockWebSocket.instances).toHaveLength(1);
    conn.disconnect();
  });

  it('transient close (1006) still reconnects normally', () => {
    const { ws, conn } = createConnected();
    ws.simulateClose(1006);
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(2);
    conn.disconnect();
  });

  it('applies exponential backoff', () => {
    const { ws, conn } = createConnected();
    // First close → 1s delay
    ws.simulateClose();
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(2);

    // Second close → 2s delay
    const ws2 = MockWebSocket.latest();
    ws2.simulateClose();
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(2); // not yet
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(3);

    conn.disconnect();
  });

  it('caps reconnect delay at 30s', () => {
    const { ws, conn } = createConnected();
    // Force many disconnects to push delay beyond 30s
    let currentWs = ws;
    for (let i = 0; i < 10; i++) {
      currentWs.simulateClose();
      vi.advanceTimersByTime(30_000);
      currentWs = MockWebSocket.latest();
      currentWs.simulateOpen();
    }
    // After 10 iterations, delay should still be at most 30s
    currentWs.simulateClose();
    vi.advanceTimersByTime(30_000);
    expect(MockWebSocket.instances.length).toBeGreaterThan(10);
    conn.disconnect();
  });

  it('resets backoff delay after successful connect', () => {
    const { ws, conn } = createConnected();
    // Disconnect and reconnect several times to increase delay
    ws.simulateClose();
    vi.advanceTimersByTime(1000);
    const ws2 = MockWebSocket.latest();
    ws2.simulateOpen();
    ws2.simulateMessage({ type: 'init' });

    // After successful init, delay should reset
    ws2.simulateClose();
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(3);
    conn.disconnect();
  });
});

describe('ProjectConnection message dispatch (the verbatim ws:* bridge)', () => {

  it('warns ONCE per unknown type (forward-compat) and does not throw', () => {
    const { ws, conn } = createConnected();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(() => {
      ws.simulateMessage({ type: 'totally_new_event', data: 'x' });
      ws.simulateMessage({ type: 'totally_new_event', data: 'x' });
      ws.simulateMessage({ type: 'another_new_event' });
    }).not.toThrow();
    const warned = warn.mock.calls.map(c => String(c[0]));
    const count = warned.filter(w => w.includes('totally_new_event')).length;
    expect(count).toBe(1); // once per type, not per message
    conn.disconnect();
  });

  it('ignores malformed JSON without error', () => {
    const { ws, conn } = createConnected();
    expect(() => {
      ws.onmessage?.({ data: 'not json' });
    }).not.toThrow();
    conn.disconnect();
  });
});
