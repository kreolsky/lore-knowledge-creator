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
  it('emit calls registered subscriber with payload', () => {
    const handler = vi.fn();
    on('navigate-to-document', handler);
    emit('navigate-to-document', { documentId: 'doc-1' });
    expect(handler).toHaveBeenCalledOnce();
    expect(handler).toHaveBeenCalledWith({ documentId: 'doc-1' });
  });

  it('emit with no subscribers does not throw', () => {
    expect(() => emit('project-deleted')).not.toThrow();
  });

  it('multiple subscribers all get called', () => {
    const handlerA = vi.fn();
    const handlerB = vi.fn();
    on('navigate-to-document', handlerA);
    on('navigate-to-document', handlerB);
    emit('navigate-to-document', { documentId: 'doc-1' });
    expect(handlerA).toHaveBeenCalledOnce();
    expect(handlerB).toHaveBeenCalledOnce();
  });

  it('off removes subscriber — no longer called on emit', () => {
    const handler = vi.fn();
    on('navigate-to-document', handler);
    off('navigate-to-document', handler);
    emit('navigate-to-document', { documentId: 'doc-1' });
    expect(handler).not.toHaveBeenCalled();
  });

  it('subscriber error does not break other subscribers', () => {
    const bad = vi.fn(() => { throw new Error('boom'); });
    const good = vi.fn();
    on('scroll-to-line', bad);
    on('scroll-to-line', good);

    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    emit('scroll-to-line', { line: 5 });
    spy.mockRestore();

    expect(good).toHaveBeenCalledOnce();
    expect(good).toHaveBeenCalledWith({ line: 5 });
  });

  it('duplicate on() with same callback is idempotent (Set)', () => {
    const handler = vi.fn();
    on('scroll-to-line', handler);
    on('scroll-to-line', handler);
    emit('scroll-to-line', { line: 1 });
    expect(handler).toHaveBeenCalledOnce();
  });

  it('off for unregistered callback is no-op', () => {
    const handler = vi.fn();
    expect(() => off('navigate-to-document', handler)).not.toThrow();
  });

  it('void-payload events work without second arg', () => {
    const handler = vi.fn();
    on('project-deleted', handler);
    emit('project-deleted');
    expect(handler).toHaveBeenCalledOnce();
    expect(handler).toHaveBeenCalledWith(undefined);
  });

  it('events with different names are independent', () => {
    const handlerA = vi.fn();
    const handlerB = vi.fn();
    on('navigate-to-document', handlerA);
    on('navigate-to-reference', handlerB);
    emit('navigate-to-document', { documentId: 'doc-1' });
    expect(handlerA).toHaveBeenCalledOnce();
    expect(handlerB).not.toHaveBeenCalled();
  });
});
