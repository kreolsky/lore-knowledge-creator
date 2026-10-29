/** ImageToolbar over an image in a REAL table cell (real widget, real active-editor and
 * stores): clicking into the size field keeps the toolbar, and the size lands in the
 * cell — never in the host document the cell's blur hands the active slot back to. */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement, act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('mermaid', () => ({
  default: { initialize: vi.fn(), render: vi.fn(async () => ({ svg: '<svg></svg>' })) },
}));

import ImageToolbar from './ImageToolbar';
import { editorExperience, renderExtensions } from './render-bundle';
import { tableDocSource } from './live-preview/table-doc-source';
import { createTable, tableAnchor } from './live-preview/table-block-model';
import { mountLazyCell } from './live-preview/table-block-widget';
import { transcludeMap } from './live-preview/effects';
import { mountView, unmountView } from '../../editor/active-editor';
import { useAppStore } from '../../store/app-store';
import { useUIStore } from '../../store/ui-store';

const IMG = '![pic|300](ref:img1)';
const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

let host: EditorView;
let ydoc: Y.Doc;
let root: Root;

beforeEach(() => {
  transcludeMap.set('img1', { kind: 'ref-image', title: 'Pic', imageUrl: 'http://localhost/x.png' });
  useAppStore.setState({ accessLevel: 'full' } as never);
  useUIStore.setState({ isPublicShare: false } as never);
  useUIStore.getState().setSelectionEmpty(true);
  ydoc = new Y.Doc();
  const id = createTable(ydoc, [['h'], [IMG]]);
  host = new EditorView({
    state: EditorState.create({
      doc: tableAnchor('T', id),
      extensions: [editorExperience({ mode: 'document' }), renderExtensions(), tableDocSource.of(ydoc)],
    }),
    parent: document.body,
  });
  mountView(host);
  const container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(createElement(ImageToolbar)));
});

afterEach(() => {
  act(() => root.unmount());
  unmountView();
  host.destroy();
  ydoc.destroy();
  transcludeMap.delete('img1');
});

const bar = () => document.querySelector('[data-testid="image-toolbar"]');

describe('ImageToolbar — selecting a cell image by a press', () => {
  const press = (el: Element) => el.dispatchEvent(new MouseEvent('mousedown', { button: 0, bubbles: true, cancelable: true }));

  it('with focus coming from the document, the toolbar stays up past the document\'s late focus notice', async () => {
    await act(async () => { host.focus(); await wait(20); });
    const cell = mountLazyCell(host.dom.querySelector('td .cm-table-block-cell-editor')!)!;
    await act(async () => {
      press(cell.contentDOM.querySelector('.cm-image-embed img')!);
      await wait(30); // CM6 notifies the document's focus loss after 10ms
    });
    expect(cell.state.selection.main.to - cell.state.selection.main.from).toBe(IMG.length);
    expect(bar()).not.toBeNull();
  });
});
