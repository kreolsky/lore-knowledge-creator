/** TDD: defines the contract for useEvent hook before implementation.
 *
 * Minimal renderHook via React.createElement + createRoot (no @testing-library/react).
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { createElement, useCallback, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

type OnFn = typeof import('../events/event-bus').on;
type OffFn = typeof import('../events/event-bus').off;
type EmitFn = typeof import('../events/event-bus').emit;
type UseEventFn = typeof import('./useEvent').useEvent;

let on: OnFn;
let off: OffFn;
let emit: EmitFn;
let useEvent: UseEventFn;

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.resetModules();
  const bus = await import('../events/event-bus');
  on = bus.on;
  off = bus.off;
  emit = bus.emit;
  const hook = await import('./useEvent');
  useEvent = hook.useEvent;

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

// Helper: import afterEach at top
import { afterEach } from 'vitest';

// ── useEvent hook ────────────────────────────────────────────────────────────

describe('useEvent hook', () => {
  it('subscribes on mount and receives events', () => {
    const handler = vi.fn();

    function TestComponent() {
      useEvent('navigate-to-document', useCallback(handler, []));
      return null;
    }

    act(() => root.render(createElement(TestComponent)));
    act(() => emit('navigate-to-document', { documentId: 'doc-1' }));

    expect(handler).toHaveBeenCalledOnce();
    expect(handler).toHaveBeenCalledWith({ documentId: 'doc-1' });
  });

  it('unsubscribes on unmount', () => {
    const handler = vi.fn();

    function TestComponent() {
      useEvent('navigate-to-document', useCallback(handler, []));
      return null;
    }

    act(() => root.render(createElement(TestComponent)));
    act(() => root.render(null));
    act(() => emit('navigate-to-document', { documentId: 'doc-1' }));

    expect(handler).not.toHaveBeenCalled();
  });

  it('re-subscribes when callback reference changes', () => {
    const handler1 = vi.fn();
    const handler2 = vi.fn();
    let setUseSecond: (v: boolean) => void;

    function TestComponent() {
      const [useSecond, _setUseSecond] = useState(false);
      setUseSecond = _setUseSecond;
      const cb = useCallback(
        (payload: { documentId: string }) => (useSecond ? handler2 : handler1)(payload),
        [useSecond],
      );
      useEvent('navigate-to-document', cb);
      return null;
    }

    act(() => root.render(createElement(TestComponent)));
    act(() => emit('navigate-to-document', { documentId: 'first' }));
    expect(handler1).toHaveBeenCalledOnce();
    expect(handler2).not.toHaveBeenCalled();

    act(() => setUseSecond!(true));
    act(() => emit('navigate-to-document', { documentId: 'second' }));
    expect(handler2).toHaveBeenCalledOnce();
  });

  it('handles void-payload events', () => {
    const handler = vi.fn();

    function TestComponent() {
      useEvent('project-deleted', useCallback(handler, []));
      return null;
    }

    act(() => root.render(createElement(TestComponent)));
    act(() => emit('project-deleted'));

    expect(handler).toHaveBeenCalledOnce();
  });
});
