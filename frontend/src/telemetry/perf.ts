/**
 * Perf producers for the telemetry channel — the "тормоза" (lag/freeze/latency) focus.
 *
 * // see SYSTEM: telemetry — perf producer. Threshold-crossing events (lag, sync-slow,
 * //         ack-slow, recovered) are shipped as raw rows; high-frequency continuous
 * //         metrics (RTT, edit→ack latency) are shipped as periodic windowed
 * //         aggregates (p50/p95/max/count) so we keep full signal without one row
 * //         per heartbeat. Volume is bounded by retention + indexing, not by dropping.
 */

import { sendTelemetry } from './telemetry';

// Thresholds past which a single slowdown is worth a raw row (the user-felt cases).
export const LONGTASK_MS = 200;
export const SYNC_SLOW_MS = 1500;
export const ACK_SLOW_MS = 1000;

// Message count past which a chat's full-history load stops being cheap and windowed
// (tail-first) loading starts to pay off. list_messages loads the WHOLE session, so a
// large count means a large payload + render on every open. This is a "grow-into" tripwire:
// when it fires in prod telemetry (perf/history-large), it's the signal to build windowed
// history loading. See the load-perf audit. Chosen at 150: today's max prod chat is ~34.
export const HISTORY_LARGE_COUNT = 150;
const AGGREGATE_WINDOW_MS = 10_000;

// WHY: lag/rtt/ack are free-running wall-clock intervals; a slept/throttled tab makes
//      one span hours (a 3h "freeze", a 210s ping). Above this ceiling the sample is a
//      clock artifact, not a real slowdown — drop it so max/percentiles stay meaningful.
//      sync-slow/recovered are operation-bracketed and legitimately long — NOT capped here.
export const PERF_SANITY_CAP_MS = 60_000;
const isPlausiblePerfMs = (ms: number): boolean => ms >= 0 && ms <= PERF_SANITY_CAP_MS;

function percentile(sorted: number[], p: number): number {
  if (sorted.length === 0) return 0;
  const idx = Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length));
  return sorted[idx];
}

/**
 * Buffers high-frequency samples and emits ONE windowed aggregate row per flush,
 * not one row per sample.
 */
export class RollingAggregate {
  private samples: number[] = [];
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(
    private readonly kind: string,
    private readonly windowMs: number = AGGREGATE_WINDOW_MS,
  ) {}

  add(ms: number): void {
    this.samples.push(ms);
    if (this.timer === null && typeof setInterval !== 'undefined') {
      this.timer = setInterval(() => this.flush(), this.windowMs);
    }
  }

  /** Emit the aggregate for the current window (if any samples) and reset. */
  flush(): void {
    if (this.samples.length === 0) {
      if (this.timer !== null) { clearInterval(this.timer); this.timer = null; }
      return;
    }
    const sorted = [...this.samples].sort((a, b) => a - b);
    sendTelemetry({
      category: 'perf',
      kind: this.kind,
      detail: {
        p50: percentile(sorted, 50),
        p95: percentile(sorted, 95),
        max: sorted[sorted.length - 1],
        count: sorted.length,
      },
    });
    this.samples = [];
  }
}

const rttAggregate = new RollingAggregate('rtt');
const ackAggregate = new RollingAggregate('ack');

/** Heartbeat RTT sample — windowed (a rising trend precedes the 4008 reap). */
export function recordRtt(ms: number): void {
  if (!isPlausiblePerfMs(ms)) return;
  rttAggregate.add(ms);
}

/**
 * Edit→ack latency — the truest "does my typing feel smooth" signal. Aggregated
 * always; a slow ack past threshold also gets a raw row.
 */
export function recordAckLatency(entityId: string, ms: number): void {
  if (!isPlausiblePerfMs(ms)) return;
  ackAggregate.add(ms);
  if (ms > ACK_SLOW_MS) {
    sendTelemetry({ category: 'perf', kind: 'ack-slow', entity_id: entityId, detail: { ms } });
  }
}

/**
 * Time-to-usable for a (re)sync. Always emits the FIRST sync after a (re)connect
 * (so "time to usable" is queryable) and any sync past the slow threshold.
 */
export function recordSyncLatency(entityId: string, ms: number, opts: { first?: boolean } = {}): void {
  if (opts.first || ms > SYNC_SLOW_MS) {
    sendTelemetry({
      category: 'perf', kind: 'sync-slow', entity_id: entityId,
      detail: { ms, first: !!opts.first },
    });
  }
}

/**
 * Reconnect OUTCOME — full drop→usable duration, distinguishing recovery from give-up.
 * `hiddenAtClose` is the visibility snapshot from socket-close time — a RELIABLE
 * benign-cause filter, unlike the ambient `wasHidden` which resets on every emit.
 */
export function recordRecovered(
  entityId: string, ms: number, opts: { gaveUp?: boolean; wasHidden?: boolean } = {},
): void {
  sendTelemetry({
    category: 'perf', kind: 'recovered', entity_id: entityId,
    detail: { ms, gaveUp: !!opts.gaveUp, hiddenAtClose: !!opts.wasHidden },
  });
}

/**
 * Full-history-load size tripwire. Emits ONE raw row when a session's loaded message
 * count crosses HISTORY_LARGE_COUNT — the point at which windowed (tail-first) loading
 * would start to pay off. Rare by construction (only large chats), so no aggregation.
 */
export function recordHistoryLoad(entityId: string, count: number): void {
  if (count >= HISTORY_LARGE_COUNT) {
    sendTelemetry({ category: 'perf', kind: 'history-large', entity_id: entityId, detail: { count } });
  }
}

let longTaskObserver: PerformanceObserver | null = null;

/** Observe main-thread long tasks; emit kind:'lag' past the threshold (UI-freeze signal). */
export function startLongTaskObserver(threshold = LONGTASK_MS): void {
  if (longTaskObserver || typeof PerformanceObserver === 'undefined') return;
  try {
    longTaskObserver = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (entry.duration > threshold && isPlausiblePerfMs(entry.duration)) {
          sendTelemetry({ category: 'perf', kind: 'lag', detail: { ms: Math.round(entry.duration) } });
        }
      }
    });
    longTaskObserver.observe({ type: 'longtask', buffered: true });
  } catch {
    // WHY: longtask is not supported everywhere (e.g. Firefox/Safari). Absence is not
    // an error — we simply lose that one signal; ack-latency + sync-slow still apply.
    longTaskObserver = null;
  }
}

export function _resetPerfForTest(): void {
  rttAggregate.flush();
  ackAggregate.flush();
  if (longTaskObserver) { longTaskObserver.disconnect(); longTaskObserver = null; }
}
