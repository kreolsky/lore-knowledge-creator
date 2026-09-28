/** TDD: defines the contract for useSwipePager before implementation.
 *
 * useSwipePager(targetRef, { onPrev, onNext }, enabled) attaches a non-passive
 * wheel listener that maps one trackpad/wheel gesture to exactly one page flip:
 * swipe up (deltaY > 0, natural scroll) → onNext; swipe down (deltaY < 0) → onPrev.
 *
 * Gesture-lock: the first event that crosses TRIGGER_DELTA flips the page and locks;
 * the rest of the gesture (incl. inertia) is swallowed until QUIET_MS of silence
 * re-arms the lock.
 *
 * Minimal renderHook via React.createElement + createRoot (no @testing-library/react),
 * same pattern as useClickOutside.test.ts. Fake timers drive the QUIET_MS unlock.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, useRef } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { useSwipePager, TRIGGER_DELTA, QUIET_MS } from './useSwipePager';

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
});

/** Dispatch `count` wheel events with the given deltas onto `el`. */
function burst(el: HTMLElement, deltaY: number, deltaX = 0, count = 5): WheelEvent[] {
  const events: WheelEvent[] = [];
  for (let i = 0; i < count; i++) {
    const ev = new WheelEvent('wheel', { deltaY, deltaX, cancelable: true, bubbles: true });
    events.push(ev);
    el.dispatchEvent(ev);
  }
  return events;
}

function renderPager(onPrev: () => void, onNext: () => void, enabled = true): HTMLDivElement {
  function Test() {
    const ref = useRef<HTMLDivElement>(null);
    useSwipePager(ref, { onPrev, onNext }, enabled);
    return createElement('div', { ref, id: 'root' });
  }
  act(() => root.render(createElement(Test)));
  return container.querySelector('#root') as HTMLDivElement;
}

describe('useSwipePager hook', () => {
  it('a burst of deltaY>0 calls onNext exactly once (gesture-lock)', () => {
    const onPrev = vi.fn();
    const onNext = vi.fn();
    const el = renderPager(onPrev, onNext);
    burst(el, 30);
    expect(onNext).toHaveBeenCalledOnce();
    expect(onPrev).not.toHaveBeenCalled();
  });

  it('a burst of deltaY<0 calls onPrev exactly once', () => {
    const onPrev = vi.fn();
    const onNext = vi.fn();
    const el = renderPager(onPrev, onNext);
    burst(el, -30);
    expect(onPrev).toHaveBeenCalledOnce();
    expect(onNext).not.toHaveBeenCalled();
  });

  it('after QUIET_MS of silence, a second burst flips again (total 2)', () => {
    const onNext = vi.fn();
    const el = renderPager(vi.fn(), onNext);
    burst(el, 30);
    expect(onNext).toHaveBeenCalledTimes(1);
    // Silence ends the gesture and unlocks.
    act(() => { vi.advanceTimersByTime(QUIET_MS + 1); });
    burst(el, 30);
    expect(onNext).toHaveBeenCalledTimes(2);
  });

  it('extra events within the same gesture (before QUIET_MS) are swallowed (still 1)', () => {
    const onNext = vi.fn();
    const el = renderPager(vi.fn(), onNext);
    burst(el, 30, 0, 12);
    // Advance partway through the quiet window and fire more — still locked.
    act(() => { vi.advanceTimersByTime(QUIET_MS - 50); });
    burst(el, 30, 0, 8);
    expect(onNext).toHaveBeenCalledOnce();
  });

  it('ignores predominantly-horizontal scrolls (|deltaX| > |deltaY|)', () => {
    const onPrev = vi.fn();
    const onNext = vi.fn();
    const el = renderPager(onPrev, onNext);
    burst(el, 2, 50);
    expect(onNext).not.toHaveBeenCalled();
    expect(onPrev).not.toHaveBeenCalled();
  });

  it('ignores events below the trigger threshold (|deltaY| < TRIGGER_DELTA)', () => {
    const onPrev = vi.fn();
    const onNext = vi.fn();
    const el = renderPager(onPrev, onNext);
    burst(el, TRIGGER_DELTA - 1);
    expect(onNext).not.toHaveBeenCalled();
    expect(onPrev).not.toHaveBeenCalled();
  });

  it('does not bind (no call, no preventDefault) when enabled=false', () => {
    const onPrev = vi.fn();
    const onNext = vi.fn();
    const el = renderPager(onPrev, onNext, false);
    const ev = burst(el, 30, 0, 1)[0];
    expect(onNext).not.toHaveBeenCalled();
    expect(onPrev).not.toHaveBeenCalled();
    expect(ev.defaultPrevented).toBe(false);
  });

  it('calls preventDefault on handled wheel events', () => {
    const el = renderPager(vi.fn(), vi.fn());
    const ev = burst(el, 30, 0, 1)[0];
    expect(ev.defaultPrevented).toBe(true);
  });

  it('releases the lock despite a sub-threshold momentum tail (trackpad inertia)', () => {
    const onNext = vi.fn();
    const el = renderPager(vi.fn(), onNext);
    // Main gesture: one above-threshold event flips a page and locks.
    el.dispatchEvent(new WheelEvent('wheel', { deltaY: 30, cancelable: true, bubbles: true }));
    expect(onNext).toHaveBeenCalledTimes(1);
    // Momentum tail: a sub-threshold inertia event partway through the quiet window.
    // It must NOT re-arm the lock, else a fresh swipe can't fire until inertia dies.
    act(() => { vi.advanceTimersByTime(QUIET_MS - 50); });
    el.dispatchEvent(new WheelEvent('wheel', { deltaY: 2, cancelable: true, bubbles: true }));
    // Now past QUIET_MS since the SIGNIFICANT motion → lock must have released.
    act(() => { vi.advanceTimersByTime(60); });
    // A fresh swipe flips again (total 2). Without the fix the sub-threshold event
    // kept the lock alive and this is swallowed.
    el.dispatchEvent(new WheelEvent('wheel', { deltaY: 30, cancelable: true, bubbles: true }));
    expect(onNext).toHaveBeenCalledTimes(2);
  });
});
