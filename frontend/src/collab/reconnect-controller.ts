/** Composable reconnect logic — owned by each WS connection class. */
// ARCH: Composition, not inheritance. Both ProjectCollabConnection and ProjectConnection
// hold a ReconnectController field. No abstract base class leaking lifecycle hooks.

export const MAX_RECONNECT_DELAY = 30_000;
export const MAX_RECONNECT_ATTEMPTS = 50;
export const INITIAL_RECONNECT_DELAY = 1000;
const JITTER_MAX_MS = 500;

function jitteredDelay(baseDelay: number): number {
  return baseDelay + Math.floor(Math.random() * JITTER_MAX_MS);
}

export class ReconnectController {
  private delay = INITIAL_RECONNECT_DELAY;
  private _attempts = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private _intentional = false;
  private _lastScheduledDelay = 0;

  get attempts(): number { return this._attempts; }
  get intentional(): boolean { return this._intentional; }
  // WHY: telemetry needs the REAL delay used (jitter included) without changing the
  // 'scheduled' | 'gave_up' return-type contract its tests assert. Read after schedule().
  get lastScheduledDelay(): number { return this._lastScheduledDelay; }

  schedule(reconnectFn: () => void): 'gave_up' | 'scheduled' {
    this._attempts++;
    if (this._attempts > MAX_RECONNECT_ATTEMPTS) {
      return 'gave_up';
    }
    const chosen = jitteredDelay(this.delay);
    this._lastScheduledDelay = chosen;
    this.timer = setTimeout(() => {
      this.timer = null;
      reconnectFn();
    }, chosen);
    this.delay = Math.min(this.delay * 2, MAX_RECONNECT_DELAY);
    return 'scheduled';
  }

  resetBackoff(): void {
    this.delay = INITIAL_RECONNECT_DELAY;
    this._attempts = 0;
  }

  cancel(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  // WHY: one-shot — once markIntentional() is called, this controller stays
  // intentional for its lifetime. Why: each WS connection instantiates a fresh
  // controller on (re)open, so we never need to reset. Treating it as resettable
  // would invite races where a stale scheduled reconnect fires after close.
  markIntentional(): void {
    this._intentional = true;
    this.cancel();
  }
}
