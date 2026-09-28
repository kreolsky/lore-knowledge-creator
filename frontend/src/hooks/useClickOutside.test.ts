/** TDD: defines the contract for useClickOutside hook before implementation.
 *
 * useClickOutside(ref, handler, enabled) attaches a mousedown listener that
 * fires handler when the pointer lands outside ref.current.
 *
 * Minimal renderHook via React.createElement + createRoot (no @testing-library/react).
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, useRef } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import type { UseClickOutsideFn } from './useClickOutside';

let useClickOutside: UseClickOutsideFn;
let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.resetModules();
  ({ useClickOutside } = await import('./useClickOutside'));
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe('useClickOutside hook', () => {
  it('fires handler on mousedown outside the ref', () => {
    const handler = vi.fn();
    function Test() {
      const ref = useRef<HTMLDivElement>(null);
      useClickOutside(ref, handler);
      return createElement('div', { ref });
    }
    act(() => root.render(createElement(Test)));

    act(() => {
      document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    });

    expect(handler).toHaveBeenCalledOnce();
  });

  it('does NOT fire when mousedown is inside the ref', () => {
    const handler = vi.fn();
    let inside: HTMLDivElement | null = null;
    function Test() {
      const ref = useRef<HTMLDivElement>(null);
      useClickOutside(ref, handler);
      return createElement('div', { ref, id: 'inner' });
    }
    act(() => root.render(createElement(Test)));
    inside = container.querySelector('#inner');

    act(() => {
      inside!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    });

    expect(handler).not.toHaveBeenCalled();
  });

  it('does not attach when enabled=false', () => {
    const handler = vi.fn();
    function Test() {
      const ref = useRef<HTMLDivElement>(null);
      useClickOutside(ref, handler, false);
      return createElement('div', { ref });
    }
    act(() => root.render(createElement(Test)));

    act(() => {
      document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    });

    expect(handler).not.toHaveBeenCalled();
  });

  it('cleans up the listener on unmount', () => {
    const handler = vi.fn();
    function Test() {
      const ref = useRef<HTMLDivElement>(null);
      useClickOutside(ref, handler);
      return createElement('div', { ref });
    }
    act(() => root.render(createElement(Test)));
    act(() => root.unmount());

    act(() => {
      document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    });

    expect(handler).not.toHaveBeenCalled();
  });
});
