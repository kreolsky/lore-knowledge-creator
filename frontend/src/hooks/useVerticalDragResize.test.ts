/** TDD: useVerticalDragResize must respond to Pointer Events (touch + mouse), not mouse-only.
 *
 * See useResizer.test.ts for the dispatch approach (jsdom has no PointerEvent ctor;
 * we dispatch MouseEvent with pointer-typed names — handlers only read clientY/target).
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { useVerticalDragResize } from './useVerticalDragResize';

let container: HTMLDivElement;
let root: Root;
let latestHeight = 0;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function pointer(type: string, target: EventTarget, clientY: number) {
  act(() => {
    target.dispatchEvent(new MouseEvent(type, { clientY, bubbles: true }));
  });
}

describe('useVerticalDragResize — pointer events', () => {
  it('resizes on pointerdown + pointermove (drag up increases height)', () => {
    let handleEl: HTMLDivElement | null = null;

    function TestComponent() {
      const r = useVerticalDragResize({ defaultHeight: 200, minHeight: 200, maxHeight: 600 });
      latestHeight = r.height;
      return createElement('div', {
        ref: (el: HTMLDivElement | null) => {
          handleEl = el;
          (r.handleRef as { current: HTMLDivElement | null }).current = el;
        },
      });
    }

    act(() => root.render(createElement(TestComponent)));

    pointer('pointerdown', handleEl!, 500);
    pointer('pointermove', document, 400); // delta = 500 - 400 = +100 → 300
    pointer('pointerup', document, 400);

    expect(latestHeight).toBe(300);
  });
});
