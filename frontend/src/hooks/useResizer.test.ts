/** TDD: useResizer must respond to Pointer Events (touch + mouse), not mouse-only.
 *
 * Minimal renderHook via React.createElement + createRoot (no @testing-library/react),
 * mirroring useEvent.test.ts. jsdom lacks a PointerEvent constructor, so we dispatch
 * MouseEvent instances with pointer-typed names — dispatch routes by the event's type
 * string, and the handlers only read clientX/clientY/target.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, useRef } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { useResizer } from './useResizer';

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function pointer(type: string, target: EventTarget, clientX: number) {
  act(() => {
    target.dispatchEvent(new MouseEvent(type, { clientX, bubbles: true }));
  });
}

describe('useResizer — pointer events', () => {
  it('resizes on pointerdown + pointermove and reports final width on pointerup', () => {
    const onWidthChange = vi.fn();
    let resizerEl: HTMLDivElement | null = null;

    function TestComponent() {
      const r = useResizer({
        side: 'left',
        defaultWidth: 220,
        minWidth: 140,
        maxWidth: 500,
        isOpen: true,
        onWidthChange,
      });
      // Attach the hook's resizerRef to a real DOM node so contains() passes.
      const localRef = useRef<HTMLDivElement | null>(null);
      return createElement('div', {
        ref: (el: HTMLDivElement | null) => {
          localRef.current = el;
          resizerEl = el;
          (r.resizerRef as { current: HTMLDivElement | null }).current = el;
        },
      });
    }

    act(() => root.render(createElement(TestComponent)));
    expect(resizerEl).not.toBeNull();

    pointer('pointerdown', resizerEl!, 100);
    pointer('pointermove', document, 160); // delta +60 → 280
    pointer('pointerup', document, 160);

    expect(onWidthChange).toHaveBeenCalledWith(280);
  });

  it('snap-closes when dragged below the snap threshold', () => {
    const onOpenChange = vi.fn();
    let resizerEl: HTMLDivElement | null = null;

    function TestComponent() {
      const r = useResizer({
        side: 'left',
        defaultWidth: 220,
        minWidth: 140,
        maxWidth: 500,
        isOpen: true,
        onOpenChange,
      });
      return createElement('div', {
        ref: (el: HTMLDivElement | null) => {
          resizerEl = el;
          (r.resizerRef as { current: HTMLDivElement | null }).current = el;
        },
      });
    }

    act(() => root.render(createElement(TestComponent)));

    pointer('pointerdown', resizerEl!, 300);
    pointer('pointermove', document, 0); // newW = 220 - 300 = -80 < threshold → close

    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
