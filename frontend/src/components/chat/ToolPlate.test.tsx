/** Tests for ToolPlate: the header row (only title + chevron toggles), the
 * single body box, collapse-on-body-click (interactive elements and text
 * selection pass through) and tone placement. */

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
  // The plate wrapper: header row + collapsible body + footer.
  return container.firstElementChild as HTMLElement | null;
}

function bodyNode(): HTMLElement | null {
  // The one bg-surface2 box — present only while expanded.
  return container.querySelector('.bg-surface2');
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

describe('ToolPlate — header row', () => {
  const iconEl = () => container.querySelector('[data-testid="icon"]')!;

  it('does NOT toggle when the icon is clicked', () => {
    mount(makePlate({ icon: createElement('span', { 'data-testid': 'icon' }, 'i'), title: 'T' }, 'body'));
    click(iconEl());
    expect(headerNode()?.getAttribute('aria-expanded')).toBe('false');
    expect(bodyNode()).toBeNull();
  });

  it('does NOT toggle when the empty rest of the header row is clicked', () => {
    mount(makePlate({ icon: 'i', title: 'T' }, 'body'));
    click(headerNode()!.parentElement!);
    expect(headerNode()?.getAttribute('aria-expanded')).toBe('false');
  });

  it('toggles when the title is clicked', () => {
    mount(makePlate({ icon: 'i', title: createElement('span', { 'data-testid': 'title' }, 'T') }, 'body'));
    click(container.querySelector('[data-testid="title"]')!);
    expect(headerNode()?.getAttribute('aria-expanded')).toBe('true');
    expect(bodyNode()).not.toBeNull();
  });

  it('keeps the icon outside the button and the chevron inside it, right after the title', () => {
    mount(makePlate({ icon: createElement('span', { 'data-testid': 'icon' }, 'i'), title: 'Title' }, 'body'));
    const btn = headerNode()!;
    expect(btn.contains(iconEl())).toBe(false);
    expect(btn.children.length).toBe(2);
    expect(btn.children[0].textContent).toBe('Title');
    expect(btn.children[1].tagName.toLowerCase()).toBe('svg');
    expect(btn.classList.contains('w-full')).toBe(false);
  });

  it('caps the body height and lets it scroll', () => {
    mount(makePlate({ icon: 'i', title: 'T', defaultExpanded: true }, 'body'));
    expect(bodyNode()!.className).toContain('max-h-[min(400px,50vh)]');
    expect(bodyNode()!.className).toContain('overflow-auto');
  });

  it('puts the failed amber border on the body, not on the header', () => {
    mount(makePlate({ icon: 'i', title: 'T', tone: 'failed', defaultExpanded: true }, 'body'));
    expect(bodyNode()!.className).toContain('border-amber-500');
    const header = headerNode()!.parentElement!;
    expect(header.className).not.toContain('border-amber-500');
    expect(plateNode()!.className).not.toContain('border-amber-500');
    expect(headerNode()!.querySelector('.text-amber-400')).not.toBeNull();
  });

  it('mutes the whole plate for noop', () => {
    mount(makePlate({ icon: 'i', title: 'T', tone: 'noop' }, 'body'));
    expect(plateNode()!.className).toContain('opacity-70');
  });
});

describe('ToolPlate — bare (group) plate', () => {
  it('has no body box and does not collapse on a click inside its body', () => {
    mount(makePlate({ icon: 'i', title: 'T', defaultExpanded: true, bare: true },
      createElement('p', { 'data-testid': 'member' }, 'member chip')));
    expect(container.querySelector('.bg-surface2')).toBeNull();
    expect(container.innerHTML).not.toContain('max-h-');
    click(container.querySelector('[data-testid="member"]')!);
    expect(headerNode()?.getAttribute('aria-expanded')).toBe('true');
  });

  it('draws the group rule under the header while collapsed, not when expanded', () => {
    mount(makePlate({ icon: 'i', title: 'T', bare: true }, 'members'));
    const header = headerNode()!.parentElement!;
    expect(header.classList.contains('border-b')).toBe(true);
    click(headerNode()!);
    expect(header.classList.contains('border-b')).toBe(false);
  });

  it('keeps a plain chip header without the group rule', () => {
    mount(makePlate({ icon: 'i', title: 'T' }, 'body'));
    expect(headerNode()!.parentElement!.classList.contains('border-b')).toBe(false);
  });

  it('closes the expanded group with a full-width rule under it', () => {
    mount(makePlate({ icon: 'i', title: 'T', defaultExpanded: true, bare: true }, 'members'));
    const body = container.querySelector('[data-testid="member"]')?.parentElement
      ?? headerNode()!.parentElement!.nextElementSibling as HTMLElement;
    expect(body.classList.contains('border-b')).toBe(true);
    expect(body.classList.contains('border-l')).toBe(false);
    expect(body.className).toContain('border-border');
  });
});
