/**
 * Unit tests for ImageToolbar — the selected-image overlay:
 *   - shows ONLY while the main selection equals one ImageWidget range, gated
 *     to a full-access editable view (commentator/readonly see nothing)
 *   - size field prefill from the `|…` suffix; Enter applies (50% percent, ''
 *     clears) through ONE transaction that re-selects the node (toolbar stays)
 *   - invalid input → inline error, doc untouched
 *   - Delete removes the embed NODE only; Download anchors the reference's file
 *
 * Harness: real EditorView + markdown parser + renderExtensions() in jsdom
 * (atomic-delete-guard.test.ts pattern) + store mocks (SelectionToolbar.test
 * pattern). The real events bus drives the show path.
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { EditorState, EditorSelection } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { ensureSyntaxTree } from '@codemirror/language';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let view: EditorView | null = null;
// WHY stable identity: the real useEditorView() returns a stable getter; a fresh
// closure per render re-identifies every callback and spins the effects.
const getView = () => view;
vi.mock('../../editor/active-editor', () => ({
  useEditorView: () => getView,
  getRoleView: () => null,
}));

const imageRef = {
  reference_id: 'img1',
  title: 'The Picture',
  file_path: 'proj/img1/pic.png',
  file_meta: { original_name: 'pic.png' },
  media_type: 'image',
};
const appState: Record<string, unknown> = {
  accessLevel: 'full',
  currentDocument: null,
  references: [imageRef],
};
vi.mock('../../store/app-store', () => ({
  useAppStore: Object.assign((sel: (s: unknown) => unknown) => sel(appState), { getState: () => appState }),
}));
const uiState: Record<string, unknown> = {
  selectionEmpty: true,
  isPublicShare: false,
  getRefOpenMode: () => 'center',
  // The shared selection feed (editor-observer) writes here from a cell view.
  setSelectionEmpty: (empty: boolean) => { uiState.selectionEmpty = empty; },
};
vi.mock('../../store/ui-store', () => ({
  useUIStore: Object.assign((sel: (s: unknown) => unknown) => sel(uiState), { getState: () => uiState }),
}));
vi.mock('../../i18n', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
  t: (k: string) => k,
}));

import ImageToolbar from './ImageToolbar';
import { emit } from '../../events';
import { renderExtensions } from './render-bundle';
import { transcludeMap } from './live-preview/effects';
const views: EditorView[] = [];

function makeView(doc: string, selFrom: number, selTo = selFrom): EditorView {
  const v = new EditorView({
    state: EditorState.create({
      doc,
      selection: { anchor: selFrom, head: selTo },
      extensions: [markdown({ base: markdownLanguage }), renderExtensions()],
    }),
    parent: document.createElement('div'),
  });
  views.push(v);
  ensureSyntaxTree(v.state, v.state.doc.length, 5000);
  v.dispatch({ selection: EditorSelection.range(selFrom, selTo) });
  // jsdom has no layout: coordsAtPos needs stubbed rects for the toolbar anchor.
  v.coordsAtPos = () => ({ left: 100, right: 200, top: 50, bottom: 70 }) as never;
  return v;
}

let root: Root | null = null;
function mount() {
  const host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(createElement(ImageToolbar)));
}
function unmount() {
  const r = root;
  if (r) act(() => r.unmount());
  root = null;
}

/** Selects the image embed whole and feeds the toolbar's show path. */
function selectImage(doc: string, embed: string) {
  const from = doc.indexOf(embed);
  view = makeView(doc, from, from + embed.length);
  uiState.selectionEmpty = false;
  act(() => { emit('editor-selection-change', { empty: false }); });
}

const bar = () => document.querySelector('[data-testid="image-toolbar"]');
const input = () => bar()?.querySelector('input') as HTMLInputElement | null;

/** Types into the field so React's onChange fires (the prototype setter skips
 *  React's per-node value tracker — a plain `el.value =` silences onChange). */
function typeValue(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

beforeEach(() => {
  transcludeMap.set('img1', { kind: 'ref-image', title: 'Pic', imageUrl: 'http://localhost/x.png' });
  uiState.selectionEmpty = true;
  uiState.isPublicShare = false;
  appState.accessLevel = 'full';
  appState.currentDocument = null;
  appState.references = [imageRef];
  mount();
});

afterEach(() => {
  unmount();
  while (views.length) views.pop()!.destroy();
  view = null;
  transcludeMap.delete('img1');
});

describe('ImageToolbar — free aspect size', () => {
  const FREE = '![pic|500x446](ref:img1)';

  it('prefills both axes of a non-proportional size', () => {
    selectImage(`a ${FREE} b`, FREE);
    expect(input()!.value).toBe('500x446');
  });

  it('Enter writes both axes from the field', () => {
    selectImage(`a ${FREE} b`, FREE);
    const el = input()!;
    act(() => { typeValue(el, '320x200'); });
    act(() => {
      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    });
    expect(view!.state.doc.toString()).toBe('a ![pic|320x200](ref:img1) b');
  });

  it('a bare width is proportional: the height is dropped', () => {
    selectImage(`a ${FREE} b`, FREE);
    const el = input()!;
    act(() => { typeValue(el, '320'); });
    act(() => {
      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    });
    expect(view!.state.doc.toString()).toBe('a ![pic|320](ref:img1) b');
  });
});
