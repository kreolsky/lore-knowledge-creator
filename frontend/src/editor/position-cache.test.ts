/** Tests for the module-level editor position cache (FIX 10). */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('../store/ui-store', () => ({
  useUIStore: {
    getState: () => ({ setDocumentPosition: vi.fn() }),
  },
}));

import { positionCacheSet, positionCacheGet, clearPositionCache, firstVisibleLine } from './position-cache';

describe('position-cache', () => {
  beforeEach(() => {
    clearPositionCache();
  });

  it('stores and retrieves a position', () => {
    positionCacheSet('doc-1', { cursor: 5, scroll: 10 });
    expect(positionCacheGet('doc-1')).toEqual({ cursor: 5, scroll: 10 });
  });

  it('clearPositionCache empties the cache (FIX 10 — no cross-user leak)', () => {
    positionCacheSet('doc-1', { cursor: 5, scroll: 10 });
    positionCacheSet('doc-2', { cursor: 0, scroll: 0 });
    expect(positionCacheGet('doc-1')).toBeDefined();
    clearPositionCache();
    expect(positionCacheGet('doc-1')).toBeUndefined();
    expect(positionCacheGet('doc-2')).toBeUndefined();
  });
});

describe('firstVisibleLine', () => {
  // Fake view: lines are 25px tall, line N starts at document height N*25 and its
  // `from` is N*100. lineBlockAtHeight takes DOCUMENT-relative height (CM6 contract).
  function fakeView(scrollTop: number, scrollerPaddingTop: string) {
    const scrollDOM = document.createElement('div');
    scrollDOM.style.paddingTop = scrollerPaddingTop;
    document.body.appendChild(scrollDOM);
    Object.defineProperty(scrollDOM, 'scrollTop', { value: scrollTop });
    return {
      scrollDOM,
      lineBlockAtHeight: (h: number) => {
        const n = Math.floor(h / 25);
        return { from: n * 100, top: n * 25 };
      },
    } as unknown as import('@codemirror/view').EditorView;
  }

  it('subtracts the scroller top padding before asking CM6 for the line', () => {
    // scrollTop 132 with a 32px scroller padding → document height 100 → line 4, cut by 0px.
    expect(firstVisibleLine(fakeView(132, '32px'))).toEqual({ from: 400, offset: 0 });
  });

  it('records how many pixels of the line sit above the visible edge', () => {
    // document height 110 → line 4 starts at 100 → 10px hidden above the edge.
    expect(firstVisibleLine(fakeView(142, '32px'))).toEqual({ from: 400, offset: 10 });
  });

  it('is the identity when the scroller has no padding', () => {
    expect(firstVisibleLine(fakeView(132, '0px'))).toEqual({ from: 500, offset: 7 });
  });

  it('keeps a negative offset while the top padding is still visible', () => {
    // scrollTop 0 with 32px padding: line 1 sits 32px BELOW the edge — restoring must
    // put it back there, not pull it up to the edge.
    expect(firstVisibleLine(fakeView(0, '32px'))).toEqual({ from: 0, offset: -32 });
  });
});
