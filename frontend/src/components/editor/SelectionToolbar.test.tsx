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
import { usePublicNotesStore } from '../../store/public-notes-store';

// WHY: jsdom lacks Range.getClientRects, and CM6's mousedown handler calls it on the
// window selection's range once a selection is non-empty (a triple click) — uncaught,
// it fails the run even though every assertion passed.
if (!Range.prototype.getClientRects) {
  Range.prototype.getClientRects = () => ({ length: 0, item: () => null }) as unknown as DOMRectList;
}

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
  uiState.isPublicShare = false;
  appState.accessLevel = 'full';
  appState.currentReference = null;
  usePublicNotesStore.setState({ allowComments: false });
});
afterEach(() => {
  unmount();
  view?.destroy();
  view = null;
});

describe('SelectionToolbar', () => {

  it('a double/triple-click selection (made ON mousedown) still opens the bar', () => {
    appState.accessLevel = 'readonly';
    uiState.isPublicShare = true;
    usePublicNotesStore.setState({ allowComments: true });
    mountToolbar();
    makeView('some **selected** text', 6, 6);
    act(() => { root?.render(createElement(SelectionToolbar)); }); // attaches the pointer listeners
    // CM6 selects the word/line inside its own contentDOM mousedown handler, and the
    // store update renders before the event reaches scrollDOM and document.
    view!.contentDOM.addEventListener('mousedown', () => {
      view!.dispatch({ selection: { anchor: 6, head: 13 } });
      uiState.selectionEmpty = false;
      act(() => { root?.render(createElement(SelectionToolbar)); });
    }, { once: true });
    // WHY no act() around it: a nested act defers the render to the outer one's end,
    // i.e. past the document listener this race is about.
    view!.contentDOM.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, detail: 2 }));
    act(() => {
      view!.contentDOM.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: 300, clientY: 200 }));
    });
    expect(document.querySelector('[data-testid="guest-note-bar"]')).not.toBeNull();
    // The third click widens the selection to the line on mousedown; it stays non-empty,
    // so nothing would re-open a bar that this press closed.
    act(() => {
      view!.contentDOM.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, detail: 3 }));
      view!.contentDOM.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: 300, clientY: 200 }));
    });
    expect(document.querySelector('[data-testid="guest-note-bar"]')).not.toBeNull();
  });

  it('a press outside the editor still dismisses the bar', () => {
    mountToolbar();
    select('some text here', 5, 9);
    act(() => { document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })); });
    expect(document.querySelector('.selection-toolbar')).toBeNull();
  });
});
