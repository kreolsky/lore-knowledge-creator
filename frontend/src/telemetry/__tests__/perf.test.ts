/** Tests for perf producers — windowed aggregate, ack-slow, recovered, longtask. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../telemetry', () => ({ sendTelemetry: vi.fn() }));

import { sendTelemetry } from '../telemetry';
import {
  recordAckLatency,
  recordRecovered,
  recordRtt,
  startLongTaskObserver,
  _resetPerfForTest,
} from '../perf';

const sent = sendTelemetry as unknown as ReturnType<typeof vi.fn>;

describe('perf producers', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    sent.mockClear();
  });
  afterEach(() => {
    _resetPerfForTest();
    sent.mockClear();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('recordRecovered carries the close-time visibility as hiddenAtClose', () => {
    recordRecovered('doc1', 5000, { wasHidden: true });
    expect(sent).toHaveBeenCalledWith(
      expect.objectContaining({ detail: expect.objectContaining({ hiddenAtClose: true }) }),
    );
  });

  it('recordRtt drops clock-artifact samples above the sanity cap from the aggregate', () => {
    recordRtt(200_000);
    recordRtt(50);
    // flush the module-level rtt aggregate explicitly
    _resetPerfForTest();
    const rtt = sent.mock.calls.find((c) => c[0].kind === 'rtt');
    expect(rtt).toBeTruthy();
    expect(rtt![0].detail).toMatchObject({ count: 1, max: 50 });
  });

  it('recordAckLatency drops clock-artifact samples above the sanity cap (no ack-slow row, excluded from aggregate)', () => {
    recordAckLatency('doc1', 200_000);
    expect(sent.mock.calls.some((c) => c[0].kind === 'ack-slow')).toBe(false);

    recordAckLatency('doc1', 30);
    // flush the module-level ack aggregate explicitly
    _resetPerfForTest();
    const ack = sent.mock.calls.find((c) => c[0].kind === 'ack');
    expect(ack).toBeTruthy();
    expect(ack![0].detail).toMatchObject({ count: 1, max: 30 });
  });

  it('a longtask whose duration is a clock artifact (above sanity cap) does NOT emit lag', () => {
    let captured: ((list: { getEntries: () => { duration: number }[] }) => void) | null = null;
    class PO {
      constructor(cb: (list: { getEntries: () => { duration: number }[] }) => void) { captured = cb; }
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal('PerformanceObserver', PO);

    startLongTaskObserver(200);
    captured!({ getEntries: () => [{ duration: 350 }, { duration: 5_000_000 }] });

    const lags = sent.mock.calls.filter((c) => c[0].kind === 'lag');
    expect(lags).toHaveLength(1);
    expect(lags[0][0].detail.ms).toBe(350);
  });
});
