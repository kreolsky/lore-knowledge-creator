/** Tests for useListCapacity — measures container height + row height and returns a
 *  height-based page size (adaptive pagination). ResizeObserver is mocked; element
 *  dimensions + computed paddings are injected (jsdom performs no layout). */
// @vitest-environment jsdom

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { createElement, type RefObject } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import { useListCapacity } from './useListCapacity';

// ── ResizeObserver mock with a fire() helper to simulate size changes ──
type ROCb = (entries: { target: HTMLElement }[]) => void;
let instances: { fire: () => void }[] = [];
let prevRO: unknown;

beforeEach(() => {
  prevRO = (globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver;
  instances = [];
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    cb: ROCb;
    observed = new Set<HTMLElement>();
    constructor(cb: ROCb) {
      this.cb = cb;
      instances.push({ fire: () => this.cb(Array.from(this.observed).map(target => ({ target }))) });
    }
    observe(el: HTMLElement) { this.observed.add(el); }
    unobserve(el: HTMLElement) { this.observed.delete(el); }
    disconnect() { this.observed.clear(); }
  };
});

afterEach(() => {
  current?.dispose();
  current = null;
  (globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver = prevRO;
});

/** Trigger every live RO callback (simulate a resize tick). */
function fireAll() {
  for (const inst of instances) inst.fire();
}

interface HarnessOpts {
  containerH: number;
  rowH: number;
  count: number;
  fallback?: number;
  padTop?: string;
  padBottom?: string;
  /** Render the container without a row child (measure bails out → fallback). */
  noRow?: boolean;
}

interface Harness {
  capacity: () => number;
  renders: () => number;
  setContainerH: (h: number) => void;
  setRowH: (h: number) => void;
  dispose: () => void;
}

let current: Harness | null = null;

function renderHarness(opts: HarnessOpts): Harness {
  const dims = { containerH: opts.containerH, rowH: opts.rowH };
  let renderCount = 0;
  let capacityVal = -1;
  const containerRef: RefObject<HTMLDivElement | null> = { current: null };

  // Override getComputedStyle: return configured paddings for our container,
  // delegate everything else to the real jsdom implementation.
  const realGCS = window.getComputedStyle.bind(window);
  const fakePad = { paddingTop: opts.padTop ?? '8px', paddingBottom: opts.padBottom ?? '12px' };
  const origDesc = Object.getOwnPropertyDescriptor(window, 'getComputedStyle');
  Object.defineProperty(window, 'getComputedStyle', {
    configurable: true,
    writable: true,
    value: (el: Element): CSSStyleDeclaration =>
      el === containerRef.current ? (fakePad as CSSStyleDeclaration) : realGCS(el),
  });

  function Probe() {
    renderCount++;
    capacityVal = useListCapacity(containerRef, null, opts.count, opts.fallback ?? 7);
    return createElement(
      'div',
      {
        ref: (el: HTMLDivElement | null) => {
          containerRef.current = el;
          if (el) Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => dims.containerH });
        },
      },
      opts.noRow ? null : createElement('div', {
        ref: (el: HTMLDivElement | null) => {
          if (el) Object.defineProperty(el, 'offsetHeight', { configurable: true, get: () => dims.rowH });
        },
      }),
    );
  }

  const host = document.createElement('div');
  document.body.appendChild(host);
  const root: Root = createRoot(host);
  act(() => root.render(createElement(Probe)));

  const harness: Harness = {
    capacity: () => capacityVal,
    renders: () => renderCount,
    setContainerH: h => { dims.containerH = h; },
    setRowH: h => { dims.rowH = h; },
    dispose: () => {
      act(() => root.unmount());
      host.remove();
      if (origDesc) Object.defineProperty(window, 'getComputedStyle', origDesc);
    },
  };
  current = harness;
  return harness;
}

describe('useListCapacity', () => {
  it('measures how many rows fit on mount (floor, exclude padding)', () => {
    // usable = 480 - 8 - 12 = 460; 460/40 = 11.5 → 11
    const h = renderHarness({ containerH: 480, rowH: 40, count: 30 });
    expect(h.capacity()).toBe(11);
  });

  it('recomputes when the container height changes (ResizeObserver)', () => {
    const h = renderHarness({ containerH: 480, rowH: 40, count: 30 });
    expect(h.capacity()).toBe(11);
    h.setContainerH(680); // usable 660; 660/40 = 16.5 → 16
    act(() => fireAll());
    expect(h.capacity()).toBe(16);
  });

  it('recomputes when the row height changes (font/zoom)', () => {
    const h = renderHarness({ containerH: 480, rowH: 40, count: 30 });
    expect(h.capacity()).toBe(11);
    h.setRowH(60); // 460/60 = 7.66 → 7
    act(() => fireAll());
    expect(h.capacity()).toBe(7);
  });

  it('clamps to a minimum of 1 when nothing fits', () => {
    const h = renderHarness({ containerH: 5, rowH: 40, count: 10 });
    expect(h.capacity()).toBe(1);
  });

  it('caps at itemCount when the container is very tall', () => {
    const h = renderHarness({ containerH: 100000, rowH: 40, count: 5 });
    expect(h.capacity()).toBe(5);
  });

  it('does not storm re-renders when the size is unchanged', () => {
    const h = renderHarness({ containerH: 480, rowH: 40, count: 30 });
    expect(h.capacity()).toBe(11);
    const settled = h.renders();
    act(() => fireAll()); // identical dims → capacity unchanged
    act(() => fireAll());
    expect(h.capacity()).toBe(11);
    expect(h.renders()).toBe(settled);
  });

  it('keeps the fallback capacity when there is no row to measure', () => {
    const h = renderHarness({ containerH: 480, rowH: 40, count: 30, fallback: 7, noRow: true });
    // No firstElementChild → measure bails out → initial Math.min(count, fallback) stays.
    expect(h.capacity()).toBe(7);
  });

  it('does not throw or recompute on resize when ResizeObserver is unavailable', () => {
    (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = undefined;
    const h = renderHarness({ containerH: 480, rowH: 40, count: 30 });
    // The one-time layout-effect measure still runs even without RO.
    expect(h.capacity()).toBe(11);
    h.setContainerH(900);
    act(() => fireAll()); // no observer instances → no-op
    expect(h.capacity()).toBe(11); // unchanged: nothing observes the resize
  });
});
