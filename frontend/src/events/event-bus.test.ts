/** TDD: defines the contract for the frontend event bus before implementation.
 *
 * Mirrors backend test_event_bus.py structure: unit tests for on/off/emit.
 * Uses vi.resetModules() + dynamic import to isolate module-level state.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';

type OnFn = typeof import('./event-bus').on;
type OffFn = typeof import('./event-bus').off;
type EmitFn = typeof import('./event-bus').emit;

let on: OnFn;
let off: OffFn;
let emit: EmitFn;

beforeEach(async () => {
  vi.resetModules();
  const bus = await import('./event-bus');
  on = bus.on;
  off = bus.off;
  emit = bus.emit;
});

// ── Core pub/sub ─────────────────────────────────────────────────────────────

describe('event-bus core', () => {

  it('emit with no subscribers does not throw', () => {
    expect(() => emit('open-sidebar-docs')).not.toThrow();
  });

  it('off for unregistered callback is no-op', () => {
    const handler = vi.fn();
    expect(() => off('navigate-to-document', handler)).not.toThrow();
  });
});
