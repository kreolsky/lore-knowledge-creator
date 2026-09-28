/** Tests for perf producers — windowed aggregate, ack-slow, recovered, longtask. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../telemetry', () => ({ sendTelemetry: vi.fn() }));

import { sendTelemetry } from '../telemetry';
import {
  RollingAggregate,
  recordAckLatency,
  recordHistoryLoad,
  recordRecovered,
  recordRtt,
  startLongTaskObserver,
  HISTORY_LARGE_COUNT,
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

  it('RollingAggregate emits ONE row with p50/p95/max/count, not one per sample', () => {
    const agg = new RollingAggregate('rtt', 10_000);
    [10, 20, 30, 40, 100].forEach((v) => agg.add(v));
    agg.flush();

    expect(sent).toHaveBeenCalledTimes(1);
    const ev = sent.mock.calls[0][0];
    expect(ev.category).toBe('perf');
    expect(ev.kind).toBe('rtt');
    expect(ev.detail).toMatchObject({ count: 5, max: 100, p50: 30, p95: 100 });
  });

  it('recordHistoryLoad emits history-large only at/above the threshold', () => {
    recordHistoryLoad('sess1', HISTORY_LARGE_COUNT - 1);
    expect(sent).not.toHaveBeenCalled();

    recordHistoryLoad('sess1', HISTORY_LARGE_COUNT);
    expect(sent).toHaveBeenCalledTimes(1);
    const ev = sent.mock.calls[0][0];
    expect(ev).toMatchObject({
      category: 'perf', kind: 'history-large', entity_id: 'sess1',
      detail: { count: HISTORY_LARGE_COUNT },
    });
  });

  it('recordAckLatency emits ack-slow past the threshold', () => {
    recordAckLatency('doc1', 1500);
    const slow = sent.mock.calls.find((c) => c[0].kind === 'ack-slow');
    expect(slow).toBeTruthy();
    expect(slow![0].entity_id).toBe('doc1');
    expect(slow![0].detail.ms).toBe(1500);
  });

  it('recordAckLatency under threshold emits no ack-slow row', () => {
    recordAckLatency('doc1', 50);
    expect(sent.mock.calls.some((c) => c[0].kind === 'ack-slow')).toBe(false);
  });

  it('recordRecovered emits the full drop→usable duration', () => {
    recordRecovered('doc1', 5000);
    expect(sent).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: 'recovered', entity_id: 'doc1',
        detail: { ms: 5000, gaveUp: false, hiddenAtClose: false },
      }),
    );
  });

  it('recordRecovered carries the close-time visibility as hiddenAtClose', () => {
    recordRecovered('doc1', 5000, { wasHidden: true });
    expect(sent).toHaveBeenCalledWith(
      expect.objectContaining({ detail: expect.objectContaining({ hiddenAtClose: true }) }),
    );
  });

  it('a longtask over threshold emits kind:lag', () => {
    let captured: ((list: { getEntries: () => { duration: number }[] }) => void) | null = null;
    class PO {
      constructor(cb: (list: { getEntries: () => { duration: number }[] }) => void) { captured = cb; }
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal('PerformanceObserver', PO);

    startLongTaskObserver(200);
    captured!({ getEntries: () => [{ duration: 350 }, { duration: 50 }] });

    const lags = sent.mock.calls.filter((c) => c[0].kind === 'lag');
    expect(lags).toHaveLength(1);
    expect(lags[0][0].detail.ms).toBe(350);
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
