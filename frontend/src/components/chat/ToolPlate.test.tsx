/** Tests for ToolPlate collapse-on-body-click: an expanded plate collapses on a
 * click anywhere, while interactive elements and text selection pass through.
 * Expanding stays header-only. */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

// Tells React 19 we're in an act-supporting test runner — silences the warning.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import { ToolPlate } from './ToolPlate';
import type { Props as ToolPlateProps } from './ToolPlate';

let container: HTMLDivElement;
let root: Root;

function mount(node: React.ReactNode) {
  act(() => { root.render(node); });
}

function plateNode(): HTMLElement | null {
  // The bg-surface2 box: header + collapsible body (footer lives outside it).
  return container.querySelector('.bg-surface2');
}

function bodyNode(): HTMLElement | null {
  return container.querySelector('.px-2.pb-2');
}

function headerNode(): HTMLButtonElement | null {
  return container.querySelector('button[aria-expanded]');
}

function click(el: Element) {
  act(() => { el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); });
}

/** createElement with children folded into props — ToolPlate's Props requires
 * `children`, so the third-arg overload is not assignable. */
function makePlate(props: Omit<ToolPlateProps, 'children'>, children: ToolPlateProps['children']) {
  return createElement(ToolPlate, { ...props, children });
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => { root.unmount(); });
  container.remove();
});

describe('ToolPlate — collapse on body click', () => {
  it('collapses an expanded plate when the body is clicked', () => {
    mount(makePlate({ icon: 'i', title: 'T', defaultExpanded: true }, createElement('p', null, 'body text')));
    expect(headerNode()?.getAttribute('aria-expanded')).toBe('true');
    expect(bodyNode()).not.toBeNull();

    click(bodyNode()!);

    expect(headerNode()?.getAttribute('aria-expanded')).toBe('false');
    expect(bodyNode()).toBeNull();
  });

  it('collapses an expanded plate when a non-interactive header area event bubbles', () => {
    mount(makePlate({ icon: 'i', title: 'T', defaultExpanded: true }, 'x'));
    click(headerNode()!);
    expect(headerNode()?.getAttribute('aria-expanded')).toBe('false');
  });

  it('expands a collapsed plate only via the header, not via body-area clicks', () => {
    mount(makePlate({ icon: 'i', title: 'T', defaultExpanded: false }, createElement('p', null, 'body text')));
    expect(bodyNode()).toBeNull();

    // Body is absent when collapsed; clicking the container must NOT expand.
    click(plateNode()!);
    expect(headerNode()?.getAttribute('aria-expanded')).toBe('false');

    click(headerNode()!);
    expect(headerNode()?.getAttribute('aria-expanded')).toBe('true');
  });

  it('keeps the plate expanded when an interactive element inside the body is clicked', () => {
    const onLink = vi.fn();
    const onBtn = vi.fn();
    mount(makePlate({ icon: 'i', title: 'T', defaultExpanded: true },
      createElement('div', null,
        createElement('a', { onClick: onLink }, 'link'),
        createElement('button', { type: 'button', 'data-testid': 'body-btn', onClick: onBtn }, 'btn'))));

    click(container.querySelector('a')!);
    expect(onLink).toHaveBeenCalledTimes(1);
    expect(headerNode()?.getAttribute('aria-expanded')).toBe('true');

    click(container.querySelector('[data-testid="body-btn"]')!);
    expect(onBtn).toHaveBeenCalledTimes(1);
    expect(headerNode()?.getAttribute('aria-expanded')).toBe('true');
  });

  it('keeps the plate expanded when the click lands on a text selection', () => {
    const selection = { toString: () => 'selected words' };
    const spy = vi.spyOn(window, 'getSelection').mockReturnValue(selection as unknown as Selection);
    mount(makePlate({ icon: 'i', title: 'T', defaultExpanded: true }, createElement('p', null, 'select me')));

    click(bodyNode()!);

    expect(headerNode()?.getAttribute('aria-expanded')).toBe('true');
    spy.mockRestore();
  });

  it('collapses on body click even while autoCollapse is armed, then stays collapsed (manualRef)', () => {
    // Streaming reasoning plate: autoCollapse armed, collapseWhen not yet true.
    const render = (streaming: boolean) =>
      mount(makePlate({ icon: 'i', title: 'T', defaultExpanded: true, autoCollapse: true, isStreaming: streaming }, 'body'));
    render(true);
    expect(headerNode()?.getAttribute('aria-expanded')).toBe('true');

    click(bodyNode()!);
    expect(headerNode()?.getAttribute('aria-expanded')).toBe('false');

    // Stream ends (trigger flips true) — the auto effect must not re-expand;
    // state stays collapsed after the manual click.
    render(false);
    expect(headerNode()?.getAttribute('aria-expanded')).toBe('false');
  });
});
