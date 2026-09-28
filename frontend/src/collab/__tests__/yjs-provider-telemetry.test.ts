/** Telemetry wiring in YjsProjectProvider — a non-auth close records a `close` log. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../collab-log', () => ({ logCollabEvent: vi.fn() }));
vi.mock('../../telemetry/perf', () => ({
  recordRtt: vi.fn(),
  recordAckLatency: vi.fn(),
  recordSyncLatency: vi.fn(),
  recordRecovered: vi.fn(),
}));

import { logCollabEvent } from '../collab-log';
import { YjsProjectProvider } from '../yjs-provider';

const logged = logCollabEvent as unknown as ReturnType<typeof vi.fn>;

class MockWebSocket {
  static readonly OPEN = 1;
  static instances: MockWebSocket[] = [];
  static Original: typeof WebSocket | null = null;
  readyState = 1;
  url = '';
  binaryType = '';
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: ArrayBuffer | string }) => void) | null = null;
  onclose: ((e: { code: number; reason: string; wasClean: boolean }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) { this.url = url; MockWebSocket.instances.push(this); }
  send() {}
  close() { this.readyState = 3; }
  simulateOpen() { this.readyState = 1; this.onopen?.(); }
  simulateClose(code: number, reason = '', wasClean = false) {
    this.readyState = 3; this.onclose?.({ code, reason, wasClean });
  }
  static latest() { return MockWebSocket.instances[MockWebSocket.instances.length - 1]; }
}

describe('YjsProjectProvider telemetry', () => {
  beforeEach(() => {
    logged.mockClear();
    MockWebSocket.instances = [];
    MockWebSocket.Original = globalThis.WebSocket;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).WebSocket = MockWebSocket;
  });
  afterEach(() => {
    if (MockWebSocket.Original) (globalThis as unknown as { WebSocket: typeof WebSocket }).WebSocket = MockWebSocket.Original;
  });

  it('logs connect, open and a close with the close code', () => {
    const provider = new YjsProjectProvider('proj-1', 'user-1');
    provider.connect();
    const ws = MockWebSocket.latest();
    ws.simulateOpen();
    ws.simulateClose(1006, 'abnormal', false);

    const kinds = logged.mock.calls.map((c) => c[1]);
    expect(kinds).toContain('connect');
    expect(kinds).toContain('open');
    const close = logged.mock.calls.find((c) => c[1] === 'close');
    expect(close).toBeTruthy();
    expect(close![3]).toMatchObject({ code: 1006, reason: 'abnormal', isAuth: false });

    provider.disconnect();
  });
});
