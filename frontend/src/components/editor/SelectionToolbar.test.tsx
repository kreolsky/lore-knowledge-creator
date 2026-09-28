/**
 * Unit tests for SelectionToolbar — the style-detection contract
 * (detectActiveStyles) exercised through the rendered active-button classes,
 * plus the role gate. A REAL CodeMirror view provides the selection; the
 * toolbar portals to document.body.
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let view: EditorView | null = null;
// WHY stable identity: the real useEditorView() returns getView, a useCallback([])
// stable across renders. A fresh closure per call would re-identify showToolbar's
// useCallback deps on every render, firing the [selectionEmpty, hide, showToolbar]
// effect each pass — setAnchor(new object) → re-render → new closure → spin (seen
// as a 99%-CPU vitest worker hang).
const getView = () => view;
vi.mock('../../editor/active-editor', () => ({
  useEditorView: () => getView,
}));
const uiState: Record<string, unknown> = { selectionEmpty: true, getRefOpenMode: () => 'center' };
vi.mock('../../store/ui-store', () => ({
  useUIStore: Object.assign((sel: (s: unknown) => unknown) => sel(uiState), { getState: () => uiState }),
}));
const appState: Record<string, unknown> = { accessLevel: 'full' };
vi.mock('../../store/app-store', () => ({
  useAppStore: Object.assign((sel: (s: unknown) => unknown) => sel(appState), { getState: () => appState }),
}));
vi.mock('../../events', () => ({ on: vi.fn(), off: vi.fn() }));
vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('./markdown-actions', () => ({
  markdownActionRegistry: { bold: vi.fn(), italic: vi.fn() },
  applyColorHighlight: vi.fn(),
  getLastHighlightColor: () => '#ec883c',
  HIGHLIGHT_COLORS: ['#ec883c', '#8ab4ff', '#8ab440', '#a978d6', '#fdd663'],
  createReference: vi.fn(),
}));
vi.mock('./markdown-actions-agent', () => ({ agentAction: vi.fn() }));

import SelectionToolbar from './SelectionToolbar';

let root: Root | null = null;
function mountToolbar() {
  const host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(createElement(SelectionToolbar)));
  return host;
}
function unmount() {
  // WHY the local const: `root` is a mutable module-level `let`, so TS drops the
  // null-narrowing inside the arrow closure passed to act().
  const r = root;
  if (r) act(() => r.unmount());
  root = null;
}

function makeView(doc: string, selFrom: number, selTo = selFrom) {
  view?.destroy();
  const parent = document.createElement('div');
  document.body.appendChild(parent);
  view = new EditorView({
    state: EditorState.create({ doc, selection: { anchor: selFrom, head: selTo } }),
    parent,
  });
  // jsdom has no layout engine: coordsAtPos relies on Range.getClientRects,
  // which jsdom lacks. Supply the geometry a real browser would produce —
  // the toolbar only needs consistent rects to anchor.
  view.coordsAtPos = () => ({ left: 100, right: 200, top: 50, bottom: 70 }) as never;
  return view;
}

/** Simulate a selection: set selectionEmpty, dispatch a selection change. */
function select(doc: string, from: number, to: number) {
  makeView(doc, from, to);
  uiState.selectionEmpty = from === to;
  act(() => {
    root?.render(createElement(SelectionToolbar));
  });
}

beforeEach(() => {
  uiState.selectionEmpty = true;
  appState.accessLevel = 'full';
});
afterEach(() => {
  unmount();
  view?.destroy();
  view = null;
});

const activeBtns = () =>
  Array.from(document.querySelectorAll('.selection-toolbar .sel-toolbar-btn.active')).map(
    (b) => b.getAttribute('title'),
  );

describe('SelectionToolbar', () => {
  it('bold selection marks the bold button active; italic NOT flagged by ** markers', () => {
    mountToolbar();
    // 8..12 is exactly "bold": slice(6,8)='**' and slice(12,14)='**' — the
    // adjacent-slice form of checkMarker.
    select('plain **bold** tail', 8, 12);
    expect(activeBtns()).toContain('boldTitle');
    expect(activeBtns()).not.toContain('italicTitle');
  });

  it('single-star emphasis marks italic only', () => {
    mountToolbar();
    select('plain *em* tail', 7, 9); // exactly "em": '*' on both sides
    expect(activeBtns()).toContain('italicTitle');
    expect(activeBtns()).not.toContain('boldTitle');
  });

  it('heading line sets no list/quote actives and detects the heading level label', () => {
    mountToolbar();
    select('## Title here', 4, 9);
    // The style dropdown button carries the current level as its label.
    const label = document.querySelector('.selection-toolbar .sel-toolbar-btn span.font-semibold');
    expect(label?.textContent).toBe('H2');
  });

  it('bullet + checkbox lines mark their list buttons active', () => {
    mountToolbar();
    select('- [x] done thing', 8, 12);
    expect(activeBtns()).toContain('checkboxListTitle');
    expect(activeBtns()).toContain('bulletListTitle');
  });

  it('commentators never see the toolbar (role gate hides formatting AND agent)', () => {
    appState.accessLevel = 'commentator';
    mountToolbar();
    select('some **selected** text', 6, 13);
    expect(document.querySelector('.selection-toolbar')).toBeNull();
  });

  it('collapsing the selection hides the toolbar again', () => {
    mountToolbar();
    select('some text here', 5, 9);
    expect(document.querySelector('.selection-toolbar')).not.toBeNull();
    select('some text here', 5, 5);
    expect(document.querySelector('.selection-toolbar')).toBeNull();
  });

  it('color picker opens with the five protocol swatches, last color marked active', () => {
    mountToolbar();
    select('some colorful text', 5, 13);
    const colorBtn = document.querySelector(
      '.selection-toolbar .sel-toolbar-btn[title="colorTitle"]',
    ) as HTMLElement;
    act(() => { colorBtn.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    const swatches = Array.from(
      document.querySelectorAll('.sel-toolbar-color-picker .sel-toolbar-color-swatch'),
    );
    // Mirrors HIGHLIGHT_COLORS in markdown-actions.ts (the document protocol —
    // hex, not CSS vars, per the plan ruling).
    expect(swatches.map((s) => s.getAttribute('aria-label'))).toEqual([
      '#ec883c', '#8ab4ff', '#8ab440', '#a978d6', '#fdd663',
    ]);
    expect(swatches[0]?.className).toContain('active');
  });
});
