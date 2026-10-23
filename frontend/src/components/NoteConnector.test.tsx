/** NoteConnector's editor-update listener: it rides the HOST view (the anchor
 * lives in the document, a focused table cell never sees the edits that move
 * it), and unmount detaches the view it attached to even after focus moved.
 * Real stores; the compartment's content is the observable — a detached
 * compartment holds the inert []. */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement, act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

import { NoteConnector } from './NoteConnector';
import { noteConnectorListenerCompartment, noteConnectorRefresh } from '../editor/note-connector-refresh';
import { mountView, unmountView, claimNestedView } from '../editor/active-editor';
import { useAppStore } from '../store/app-store';
import { useNoteStore } from '../store/note-store';
import { useUIStore } from '../store/ui-store';

function makeView(): EditorView {
  return new EditorView({ state: EditorState.create({ doc: 'text', extensions: [noteConnectorRefresh] }) });
}

function isAttached(view: EditorView): boolean {
  const ext = noteConnectorListenerCompartment.get(view.state);
  return !(Array.isArray(ext) && ext.length === 0);
}

let host: EditorView;
let cell: EditorView;
let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  // The compute itself is not under test — keep the scheduled frame from running.
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
  host = makeView();
  cell = makeView();
  mountView(host);
  useAppStore.setState({ currentDocument: { document_id: 'd1' } as never, currentReference: null });
  useUIStore.getState().setRightPanelTab('d1', 'notes');
  useNoteStore.setState({ activeNoteThreadId: 'n1', connectedNoteId: 'n1' });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => { root.unmount(); });
  container.remove();
  unmountView();
  host.destroy();
  cell.destroy();
  vi.unstubAllGlobals();
});

describe('NoteConnector — editor-update listener', () => {
  it('attaches to the host view even while a table cell holds focus', () => {
    claimNestedView(cell);
    act(() => { root.render(createElement(NoteConnector)); });
    expect(isAttached(host)).toBe(true);
    expect(isAttached(cell)).toBe(false);
  });

  it('unmount detaches the view it attached to, after focus moved to a cell', () => {
    act(() => { root.render(createElement(NoteConnector)); });
    expect(isAttached(host)).toBe(true);
    claimNestedView(cell);
    act(() => { root.unmount(); });
    root = createRoot(container);
    expect(isAttached(host)).toBe(false);
  });
});
