/** PromptRail — current tick tracking, hover/tap opening of the navigator, and its rows. */
// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { createElement, createRef, type RefObject } from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

import { PromptRail, answerPreview, promptTitle, type PromptRailItem } from './PromptRail';

const ITEMS: PromptRailItem[] = [
  { id: 'u1', prompt: '\n  First prompt\nsecond line', answer: '## Heading\n- **bold** answer' },
  { id: 'u2', prompt: 'Second prompt', answer: '' },
  { id: 'u3', prompt: 'Third prompt', answer: 'plain' },
];
// Content-space tops of the user rows; the container is 400 tall at viewport top 0.
const TOPS: Record<string, number> = { u1: 100, u2: 900, u3: 1500 };

function box(top: number): DOMRect {
  return { top, bottom: top + 20, left: 0, right: 0, width: 0, height: 20, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
}

function mount(items = ITEMS) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const container = document.createElement('div');
  let scrollTop = 0;
  Object.defineProperty(container, 'scrollTop', { configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v; } });
  container.getBoundingClientRect = () => box(0);
  for (const item of items) {
    const row = document.createElement('div');
    row.dataset.messageId = item.id;
    row.getBoundingClientRect = () => box(TOPS[item.id] - scrollTop);
    container.appendChild(row);
  }
  host.appendChild(container);
  const containerRef = createRef<HTMLDivElement>() as RefObject<HTMLDivElement>;
  (containerRef as { current: HTMLDivElement }).current = container;
  const onJump = vi.fn((id: string) => { scrollTop = TOPS[id] - 12; });
  const railHost = document.createElement('div');
  host.appendChild(railHost);
  const root = createRoot(railHost);
  act(() => root.render(createElement(PromptRail, { items, containerRef, onJump })));
  return {
    host, container, onJump,
    scrollTo: async (v: number) => {
      scrollTop = v;
      container.dispatchEvent(new Event('scroll'));
      await act(async () => { await new Promise(r => requestAnimationFrame(() => r(null))); });
    },
    unmount: () => { act(() => root.unmount()); host.remove(); },
  };
}

const rail = (host: HTMLElement) => host.querySelector<HTMLElement>('[data-prompt-rail]')!;
const panel = (host: HTMLElement) => host.querySelector('[role="dialog"]');
const currentTick = (host: HTMLElement) => host.querySelector<HTMLElement>('[data-prompt-tick][aria-current]')?.dataset.promptTick;

function pointer(target: Element, type: string, pointerType: string) {
  const ev = new MouseEvent(type, { bubbles: true, cancelable: true, relatedTarget: document.body });
  Object.defineProperty(ev, 'pointerType', { value: pointerType });
  act(() => { target.dispatchEvent(ev); });
}

afterEach(() => { vi.useRealTimers(); });

describe('PromptRail — rail', () => {
  it('renders nothing with fewer than two prompts', () => {
    const { host, unmount } = mount([ITEMS[0]]);
    expect(host.querySelector('[data-prompt-rail]')).toBeNull();
    unmount();
  });

  it('the current tick is the last prompt at or above the viewport top, the first when none is', async () => {
    const { host, scrollTo, unmount } = mount();
    expect(currentTick(host)).toBe('u1');
    await scrollTo(900 - 12);
    expect(currentTick(host)).toBe('u2');
    await scrollTo(1200);
    expect(currentTick(host)).toBe('u2');
    await scrollTo(1600);
    expect(currentTick(host)).toBe('u3');
    await scrollTo(0);
    expect(currentTick(host)).toBe('u1');
    unmount();
  });

  it('a mouse click on a tick jumps to that prompt and marks it current', () => {
    const { host, onJump, unmount } = mount();
    act(() => host.querySelector<HTMLElement>('[data-prompt-tick="u3"]')!.click());
    expect(onJump).toHaveBeenCalledWith('u3');
    expect(currentTick(host)).toBe('u3');
    unmount();
  });
});

describe('PromptRail — navigator panel', () => {
  it('mouse: enter opens, leave closes after the travel delay, re-enter cancels the close', () => {
    vi.useFakeTimers();
    const { host, unmount } = mount();
    pointer(rail(host), 'pointerover', 'mouse');
    expect(panel(host)).not.toBeNull();
    pointer(rail(host), 'pointerout', 'mouse');
    act(() => { vi.advanceTimersByTime(100); });
    pointer(rail(host), 'pointerover', 'mouse');
    act(() => { vi.advanceTimersByTime(200); });
    expect(panel(host)).not.toBeNull();
    pointer(rail(host), 'pointerout', 'mouse');
    act(() => { vi.advanceTimersByTime(149); });
    expect(panel(host)).not.toBeNull();
    act(() => { vi.advanceTimersByTime(1); });
    expect(panel(host)).toBeNull();
    unmount();
  });

  it('touch: a tap toggles the panel, a tap outside closes it, a row tap jumps and closes', () => {
    const { host, onJump, unmount } = mount();
    pointer(rail(host), 'pointerover', 'touch');
    expect(panel(host)).toBeNull();
    pointer(rail(host), 'pointerdown', 'touch');
    act(() => rail(host).click());
    expect(panel(host)).not.toBeNull();
    act(() => rail(host).click());
    expect(panel(host)).toBeNull();
    act(() => rail(host).click());
    act(() => { document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })); });
    expect(panel(host)).toBeNull();
    act(() => rail(host).click());
    act(() => host.querySelector<HTMLElement>('[data-prompt-row="u2"]')!.click());
    expect(onJump).toHaveBeenCalledWith('u2');
    expect(panel(host)).toBeNull();
    unmount();
  });

  it('a touch tap on a tick opens the panel instead of jumping', () => {
    const { host, onJump, unmount } = mount();
    pointer(rail(host), 'pointerdown', 'touch');
    act(() => host.querySelector<HTMLElement>('[data-prompt-tick="u2"]')!.click());
    expect(onJump).not.toHaveBeenCalled();
    expect(panel(host)).not.toBeNull();
    unmount();
  });

  it('mouse: a row click jumps and keeps the panel open; the current row follows', async () => {
    const { host, onJump, scrollTo, unmount } = mount();
    pointer(rail(host), 'pointerover', 'mouse');
    act(() => host.querySelector<HTMLElement>('[data-prompt-row="u3"]')!.click());
    expect(onJump).toHaveBeenLastCalledWith('u3');
    expect(panel(host)).not.toBeNull();
    expect(host.querySelector('[data-prompt-row][aria-current]')?.getAttribute('data-prompt-row')).toBe('u3');
    await scrollTo(0);
    expect(host.querySelector('[data-prompt-row][aria-current]')?.getAttribute('data-prompt-row')).toBe('u1');
    unmount();
  });

  it('a row shows the prompt first line and the answer preview, none when the answer is empty', () => {
    const { host, unmount } = mount();
    pointer(rail(host), 'pointerover', 'mouse');
    const row = (id: string) => host.querySelector<HTMLElement>(`[data-prompt-row="${id}"]`)!;
    expect(row('u1').children[0].textContent).toBe('First prompt');
    expect(row('u1').children[1].textContent).toBe('Heading bold answer');
    expect(row('u2').children.length).toBe(1);
    unmount();
  });
});

describe('PromptRail — text helpers', () => {
  it('promptTitle takes the first non-empty line', () => {
    expect(promptTitle('\n \nhello\nworld')).toBe('hello');
  });

  it('answerPreview drops block markers and emphasis', () => {
    expect(answerPreview('> quote\n1. item\n`code` and __x__')).toBe('quote item code and x');
  });
});
