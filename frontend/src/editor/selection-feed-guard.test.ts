/** The one selection feed and the activation guard over it: every editor context that
 * can become the active view carries `selectionFeedPlugin` (the toolbars read only the
 * active view), a view without it cannot be activated, and the offscreen cell placeholder
 * renderer never publishes. Real EditorViews over the real context factories. */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { EditorState, type Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import { claimFocus, claimNestedView, mountView, unmountView, getEditorView } from './active-editor';
import { selectionFeedPlugin } from './editor-observer';
import { editorExperience, editableCellExtensions, renderExtensions } from '../components/editor/render-bundle';
import { buildPublicEditorExtensions } from '../components/PublicEditor';
import { tableDocSource } from '../components/editor/live-preview/table-doc-source';
import { createTable, tableAnchor } from '../components/editor/live-preview/table-block-model';
import { mountLazyCell } from '../components/editor/live-preview/table-block-widget';

const views: EditorView[] = [];
function makeView(extensions: Extension, doc = 'hello world'): EditorView {
  const v = new EditorView({ state: EditorState.create({ doc, extensions }), parent: document.body });
  views.push(v);
  return v;
}

afterEach(() => {
  vi.restoreAllMocks();
  unmountView();
  while (views.length) views.pop()!.destroy();
});

const ACTIVATORS: Record<string, (v: EditorView) => void> = {
  claimFocus: (v) => claimFocus({ role: 'primary', isReference: false, view: v, handle: null }),
  claimNestedView,
  mountView,
};

describe('activation guard — a view without the selection feed cannot become the active editor', () => {
  for (const [name, activate] of Object.entries(ACTIVATORS)) {
    it(`${name} throws on a bare view`, () => {
      const bare = makeView([]);
      expect(() => activate(bare)).toThrow(/selectionFeedPlugin/);
    });
  }
});

describe('every editor context carries the feed', () => {
  const CONTEXTS: Record<string, () => Extension> = {
    document: () => editorExperience({ mode: 'document' }),
    'editable cell': () => editableCellExtensions(),
    'public page': () => buildPublicEditorExtensions({ tablesJson: null }),
  };
  for (const [name, build] of Object.entries(CONTEXTS)) {
    it(`${name}: activates through every mutator`, () => {
      const v = makeView(build());
      expect(v.plugin(selectionFeedPlugin)).not.toBeNull();
      for (const activate of Object.values(ACTIVATORS)) expect(() => activate(v)).not.toThrow();
    });
  }
});

describe('host destroyed while a table cell holds focus', () => {
  it('the cell blur does not hand the active slot back to the dying host', () => {
    const ydoc = new Y.Doc();
    const id = createTable(ydoc, [['h1'], ['a']]);
    const host = new EditorView({
      state: EditorState.create({
        doc: tableAnchor('T', id),
        extensions: [editorExperience({ mode: 'document' }), renderExtensions(), tableDocSource.of(ydoc)],
      }),
      parent: document.body,
    });
    const cell = mountLazyCell(host.dom.querySelector('td .cm-table-block-cell-editor')!)!;
    cell.focus();
    claimNestedView(cell);
    // CM6 swallows an update-listener throw into console.error — the guard firing is the failure.
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
    host.destroy();
    expect(errors.mock.calls.flat().join(' ')).not.toMatch(/selectionFeedPlugin/);
    expect(getEditorView()).not.toBe(host);
    ydoc.destroy();
  });
});

