/**
 * Generic client telemetry sender — batched, fire-and-forget shipping to
 * POST /api/telemetry (the queryable telemetry_event table).
 *
 * // SYSTEM: telemetry — first-party diagnostics channel. Producers: collab-log.ts
 * //         (reconnects) and perf.ts (lags/latency).
 * // ARCH: REST is independent of the collab WS, so events still reach the server
 * //       during a WS-only outage (the common case). Total network drop falls back
 * //       to the ring buffer + the next pagehide sendBeacon.
 */

export type TelemetryCategory = 'collab' | 'perf';

export interface TelemetryEventInput {
  category: TelemetryCategory;
  kind: string;
  project_id?: string;
  entity_id?: string;
  detail?: Record<string, unknown>;
}

interface TelemetryRecord extends TelemetryEventInput {
  detail: Record<string, unknown>;
  client_ts: string;
}

const ENDPOINT = '/api/telemetry';
const FLUSH_DEBOUNCE_MS = 2000;
const MAX_BATCH = 50;
const RING_CAP = 100;
// Bound the retry queue so a persistent upload failure can't grow memory unbounded.
const QUEUE_CAP = 200;

// Per-tab session id so one user's reconnect storm reads as a single trace.
const SESSION_ID = Math.random().toString(36).slice(2, 12);

let queue: TelemetryRecord[] = [];
let ring: TelemetryRecord[] = [];
let flushTimer: ReturnType<typeof setTimeout> | null = null;
let wasHiddenSinceLast = false;
let listenersBound = false;

function ambientDetail(): Record<string, unknown> {
  const out: Record<string, unknown> = { session_id: SESSION_ID };
  if (typeof document !== 'undefined') {
    out.visibility = document.visibilityState;
    out.wasHidden = wasHiddenSinceLast;
  }
  // navigator.connection is non-standard / partial — read defensively.
  const nav = typeof navigator !== 'undefined' ? (navigator as unknown as {
    connection?: { effectiveType?: string; downlink?: number; rtt?: number };
  }) : undefined;
  const conn = nav?.connection;
  if (conn) {
    out.net = { effectiveType: conn.effectiveType, downlink: conn.downlink, rtt: conn.rtt };
  }
  return out;
}

function bindUnloadListeners(): void {
  if (listenersBound || typeof window === 'undefined') return;
  listenersBound = true;
  // INVARIANT: flush on hidden/unload via sendBeacon — collab closes often coincide
  // with the user leaving the tab, and a debounced fetch would be cancelled on unload.
  // Why: sendBeacon survives document unload; keepalive fetch does not reliably.
  const onHide = () => {
    if (typeof document !== 'undefined') wasHiddenSinceLast = true;
    flushBeacon();
  };
  window.addEventListener('pagehide', onHide);
  window.addEventListener('visibilitychange', () => {
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') onHide();
  });
}

/** Queue a telemetry event for batched shipping. Never throws. */
export function sendTelemetry(ev: TelemetryEventInput): void {
  const rec: TelemetryRecord = {
    category: ev.category,
    kind: ev.kind,
    project_id: ev.project_id,
    entity_id: ev.entity_id,
    detail: { ...ambientDetail(), ...(ev.detail ?? {}) },
    client_ts: new Date().toISOString(),
  };
  wasHiddenSinceLast = false;

  queue.push(rec);
  if (queue.length > QUEUE_CAP) queue = queue.slice(queue.length - QUEUE_CAP);
  ring.push(rec);
  if (ring.length > RING_CAP) ring = ring.slice(ring.length - RING_CAP);

  bindUnloadListeners();

  if (queue.length >= MAX_BATCH) {
    void flushTelemetry();
    return;
  }
  if (flushTimer === null) {
    flushTimer = setTimeout(() => { void flushTelemetry(); }, FLUSH_DEBOUNCE_MS);
  }
}

/** Force-ship the current queue in one batched POST. Swallows upload errors. */
export async function flushTelemetry(): Promise<void> {
  if (flushTimer !== null) { clearTimeout(flushTimer); flushTimer = null; }
  if (queue.length === 0) return;
  const batch = queue.slice(0, MAX_BATCH);
  queue = queue.slice(MAX_BATCH);
  try {
    await fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events: batch }),
      keepalive: true,
      credentials: 'include',
    });
  } catch {
    // WHY: telemetry is non-user-facing — never surface an upload failure. Re-queue
    // (bounded) so a transient drop retries on the next flush; the ring buffer keeps
    // the dev affordance regardless.
    queue = [...batch, ...queue].slice(0, QUEUE_CAP);
  }
  // If the queue still holds events (>MAX_BATCH this round), keep draining.
  if (queue.length > 0 && flushTimer === null) {
    flushTimer = setTimeout(() => { void flushTelemetry(); }, FLUSH_DEBOUNCE_MS);
  }
}

/** Ship via navigator.sendBeacon (survives unload). Falls back to flush if unavailable. */
export function flushBeacon(): void {
  if (queue.length === 0) return;
  const batch = queue.slice(0, MAX_BATCH);
  const payload = JSON.stringify({ events: batch });
  const beacon = typeof navigator !== 'undefined' ? navigator.sendBeacon?.bind(navigator) : undefined;
  if (beacon) {
    const ok = beacon(ENDPOINT, new Blob([payload], { type: 'application/json' }));
    if (ok) { queue = queue.slice(MAX_BATCH); return; }
  }
  void flushTelemetry();
}

/** Read-only snapshot of the recent-events ring buffer (dev/test affordance). */
export function getTelemetryBuffer(): TelemetryRecord[] {
  return [...ring];
}

/** Test-only reset of all module state. */
export function _resetTelemetryForTest(): void {
  queue = [];
  ring = [];
  if (flushTimer !== null) { clearTimeout(flushTimer); flushTimer = null; }
  wasHiddenSinceLast = false;
}

// WHY: expose the ring buffer on window so we can read live-user telemetry from a
// DevTools console without a network round-trip during a support session.
if (typeof window !== 'undefined') {
  (window as unknown as { __telemetry?: () => TelemetryRecord[] }).__telemetry = getTelemetryBuffer;
}

export { SESSION_ID };
