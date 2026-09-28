/** Tests for the generic telemetry sender — batching, ring buffer, failure swallow, beacon. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  sendTelemetry,
  flushTelemetry,
  flushBeacon,
  getTelemetryBuffer,
  _resetTelemetryForTest,
} from '../telemetry';

describe('telemetry sender', () => {
  beforeEach(() => {
    _resetTelemetryForTest();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('debounced flush issues ONE batched POST with all queued events', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal('fetch', fetchMock);

    sendTelemetry({ category: 'collab', kind: 'close', detail: { code: 4008 } });
    sendTelemetry({ category: 'perf', kind: 'lag', detail: { ms: 300 } });
    expect(fetchMock).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(2000);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.events).toHaveLength(2);
    expect(body.events[0].kind).toBe('close');
    expect(body.events[0].client_ts).toBeTruthy();
  });

  it('ring buffer caps at 100', () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true }));
    for (let i = 0; i < 130; i++) {
      sendTelemetry({ category: 'perf', kind: 'rtt', detail: { i } });
    }
    expect(getTelemetryBuffer()).toHaveLength(100);
  });

  it('swallows an upload failure without throwing and keeps the queue', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('network'));
    vi.stubGlobal('fetch', fetchMock);

    sendTelemetry({ category: 'collab', kind: 'close', detail: {} });
    await expect(flushTelemetry()).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('flushBeacon ships via navigator.sendBeacon and drains the queue', () => {
    const beacon = vi.fn().mockReturnValue(true);
    vi.stubGlobal('navigator', { sendBeacon: beacon });

    sendTelemetry({ category: 'collab', kind: 'close', detail: {} });
    flushBeacon();

    expect(beacon).toHaveBeenCalledTimes(1);
    expect(beacon.mock.calls[0][0]).toBe('/api/telemetry');
    // queue drained — a follow-up flush is a no-op (no fetch).
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal('fetch', fetchMock);
    void flushTelemetry();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
