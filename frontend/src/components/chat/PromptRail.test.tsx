/** PromptRail — current tick tracking, hover/tap opening of the navigator, and its rows. */
// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { createElement, createRef, type RefObject } from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

import {
  PromptRail, answerPreview, listScrollTarget, segmentAt, centredStart, lensFor, promptTitle, followWindow, lensBell, tickAt, windowEdges, windowLayout, type PromptRailItem,
} from './PromptRail';

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

  it('at rest every tick is half its hovered length; hovering the rail restores full length', () => {
    const { host, unmount } = mount();
    const widths = () => [...host.querySelectorAll<HTMLElement>('[data-prompt-tick] > div')].map(bar => parseFloat(bar.style.width));
    const rest = widths();
    pointer(rail(host), 'pointerover', 'mouse');
    expect(rest).toEqual(widths().map(w => Math.round(w / 2)));
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

describe('PromptRail — list follows the pointer', () => {
  function list(scrollHeight: number) {
    const el = document.createElement('div');
    let scrollTop = 0;
    Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v; } });
    Object.defineProperty(el, 'scrollHeight', { value: scrollHeight });
    Object.defineProperty(el, 'clientHeight', { value: 300 });
    el.getBoundingClientRect = () => box(100);
    const row = (contentTop: number) => {
      const r = document.createElement('div');
      r.getBoundingClientRect = () => box(100 + contentTop - scrollTop);
      return r;
    };
    return { el, row, setScroll: (v: number) => { scrollTop = v; } };
  }

  it('segmentAt gives the bracketing centres and the share between them, clamped at the ends', () => {
    const centres = [6, 18, 30];
    expect(segmentAt(centres, 12)).toEqual({ a: 0, t: 0.5 });
    expect(segmentAt(centres, 18)).toEqual({ a: 1, t: 0 });
    expect(segmentAt(centres, 0)).toEqual({ a: 0, t: 0 });
    expect(segmentAt(centres, 99)).toEqual({ a: 1, t: 1 });
  });

  it('at a tick centre the row centre lands on the pointer; between ticks the scroll interpolates', () => {
    const { el, row, setScroll } = list(2000);
    setScroll(40);
    const a = row(500); // centre 510 in content space
    const b = row(560); // centre 570
    // Pointer at viewport 250 = 150 into the list.
    expect(listScrollTarget(el, a, b, 0, 250)).toBe(360);
    expect(listScrollTarget(el, a, b, 1, 250)).toBe(420);
    expect(listScrollTarget(el, a, b, 0.5, 250)).toBe(390);
  });

  it('the target is clamped to the scroll range', () => {
    const { el, row } = list(400);
    expect(listScrollTarget(el, row(0), row(60), 0, 390)).toBe(0);
    expect(listScrollTarget(el, row(340), row(380), 1, 110)).toBe(100);
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

describe('PromptRail — lens window', () => {
  const COUNT = 80;
  const PITCH = 12;
  const HEIGHT = 300;
  const lens = lensFor(COUNT, PITCH, HEIGHT);

  it.each([COUNT, 200])('%i ticks: the clear gap between drawn ticks tapers from the full pitch to the overview gap', count => {
    const lens = lensFor(count, PITCH, HEIGHT);
    const overviewClear = lens.stride * lens.compressed - 1;
    expect(lens.size).toBe(11);
    if (count === 200) expect(lens.stride).toBeGreaterThan(1);
    expect(lens.stride * lens.compressed).toBeGreaterThanOrEqual(4);
    for (const start of [0, 17, count - lens.size]) {
      const { centres, edges, thickness } = windowLayout(count, PITCH, lens, start);
      expect(edges[0]).toBe(0);
      expect(edges[count]).toBeCloseTo(HEIGHT, 6);
      edges.slice(1).forEach((e, i) => expect(e).toBeGreaterThan(edges[i]));
      const centre = start + (lens.size - 1) / 2;
      const inside = (i: number) => i >= start && i < start + lens.size;
      const drawn = Array.from({ length: count }, (_, i) => i).filter(i => inside(i) || i % lens.stride === 0);
      const clear = (a: number, b: number) => centres[b] - centres[a] - (thickness[a] + thickness[b]) / 2;
      expect(clear(centre, centre + 1)).toBeCloseTo(PITCH - 3, 6);
      for (let k = 1; k < drawn.length; k++) {
        const [a, b] = [drawn[k - 1], drawn[k]];
        expect(clear(a, b)).toBeGreaterThanOrEqual(overviewClear - 1e-9);
        if (inside(a) && inside(b) && a >= centre) expect(clear(a, b)).toBeLessThanOrEqual(clear(a - 1, a) + 1e-9);
        if (inside(a) && inside(b) && b <= centre) expect(clear(a, b)).toBeLessThanOrEqual(clear(b, b + 1) + 1e-9);
      }
      if (lens.stride === 1 && start > 0) expect(clear(start - 1, start)).toBeCloseTo(overviewClear, 6);
    }
  });

  it('the bell peaks at the focus, falls strictly to the radius and is 0 past it', () => {
    expect(lensBell(0, 5)).toBe(1);
    for (let d = 1; d <= 5; d++) expect(lensBell(d, 5)).toBeLessThan(lensBell(d - 1, 5));
    expect(lensBell(5, 5)).toBeGreaterThan(0);
    expect(lensBell(6, 5)).toBe(0);
  });

  it('a tick outside the window keeps its position wherever the window is', () => {
    const a = windowEdges(COUNT, PITCH, lens, 10);
    const b = windowEdges(COUNT, PITCH, lens, 40);
    for (const i of [0, 5, 70, 79]) expect(a[i]).toBeCloseTo(b[i], 9);
  });

  it('a pointer still over the focus tick keeps it', () => {
    const start = centredStart(COUNT, lens, 20);
    const edges = windowEdges(COUNT, PITCH, lens, start);
    const y = (edges[20] + edges[21]) / 2;
    expect(followWindow(COUNT, PITCH, lens, 20, y)).toEqual({ start, index: 20 });
  });

  it('anywhere on the rail, the window is centred on the tick drawn under the pointer', () => {
    let index = 0;
    for (let y = 0; y < HEIGHT; y += 1.7) {
      const next = followWindow(COUNT, PITCH, lens, index, y);
      expect(next.start).toBe(centredStart(COUNT, lens, next.index));
      expect(tickAt(windowEdges(COUNT, PITCH, lens, next.start), y)).toBe(next.index);
      expect(next.index).toBeGreaterThanOrEqual(index); // moving down never steps back
      index = next.index;
    }
    expect(index).toBe(COUNT - 1);
  });

  it('at rest the window centres the current prompt, clamped to the rail ends', () => {
    expect(centredStart(COUNT, lens, 0)).toBe(0);
    expect(centredStart(COUNT, lens, 40)).toBe(40 - Math.floor(lens.size / 2));
    expect(centredStart(COUNT, lens, COUNT - 1)).toBe(COUNT - lens.size);
  });
});
