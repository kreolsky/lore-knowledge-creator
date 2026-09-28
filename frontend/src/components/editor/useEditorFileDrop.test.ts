/**
 * CSV-drop table targeting for useEditorFileDrop — split view: the dropped CSV
 * must create its table model in the DROP VIEW'S entity ydoc, never the focused
 * slot (which may hold the other column's handle). Mirrors paste-handler's
 * targeting contract.
 */
// @vitest-environment jsdom
import { describe, it, expect, afterEach, vi } from 'vitest';
import { createElement, useRef } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { EditorView } from '@codemirror/view';
import { EditorState } from '@codemirror/state';
import * as Y from 'yjs';
import type { EditorView as EditorViewType } from '@codemirror/view';

const showToast = vi.hoisted(() => vi.fn());

vi.mock('../../store/app-store', () => ({
  useAppStore: { getState: () => ({ showToast, accessLevel: 'full' }) },
}));
vi.mock('../../i18n', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

import { useEditorFileDrop } from './useEditorFileDrop';
import { setEntityHandle } from '../../collab/active-handle-registry';
import { releaseHandle, publishHandle, setViewEntity } from '../../editor/active-editor';
import type { EntityYjsState } from '../../collab/yjs-provider';
import { readTableModel, getTablesMap, TABLE_ANCHOR_RE } from './live-preview/table-block-model';
import type { Document, Reference } from '../../types';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DOC_ID = 'doc-drop-1';

function makeHandle(doc: Y.Doc): EntityYjsState {
  return { ydoc: doc } as unknown as EntityYjsState;
}

let capturedExt: { fileDropExtension: unknown } | null = null;
let editorViewRef: { current: EditorViewType | null } | null = null;
let root: Root | null = null;

function mountHook() {
  const container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  function Harness() {
    const viewRef = useRef<EditorViewType | null>(null);
    editorViewRef = viewRef;
    const activeItemRef = useRef<Document | Reference | null>(null);
    const result = useEditorFileDrop({
      editorViewRef: viewRef,
      activeItemRef,
      getDropGuard: () => ({ isReadonly: false, editable: true }),
    } as unknown as Parameters<typeof useEditorFileDrop>[0]);
    capturedExt = result;
    return createElement('div');
  }
  act(() => root!.render(createElement(Harness)));
}

function makeDropEvent(file: File): Event {
  const event = new Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', {
    value: { types: ['Files'], files: [file] },
    configurable: true,
  });
  Object.defineProperty(event, 'clientX', { value: 0, configurable: true });
  Object.defineProperty(event, 'clientY', { value: 0, configurable: true });
  return event;
}

afterEach(() => {
  const r = root;
  if (r) act(() => r.unmount());
  root = null;
  releaseHandle();
  setEntityHandle(DOC_ID, null);
});

describe('useEditorFileDrop — CSV drop targeting (split view)', () => {
  it('creates the table in the DROP VIEW\'S entity ydoc, not the slot', async () => {
    mountHook();

    const entityDoc = new Y.Doc();
    setEntityHandle(DOC_ID, makeHandle(entityDoc));
    const foreignDoc = new Y.Doc();
    publishHandle(makeHandle(foreignDoc));

    const view = new EditorView({
      state: EditorState.create({
        doc: 'line one\n',
        extensions: [capturedExt!.fileDropExtension as never],
      }),
      parent: document.body,
    });
    editorViewRef!.current = view;
    setViewEntity(view, DOC_ID);

    const file = new File(['h1,h2\na,b\n'], 'data.csv', { type: 'text/csv' });
    await act(async () => {
      view.contentDOM.dispatchEvent(makeDropEvent(file));
      await new Promise(r => setTimeout(r, 20));
    });

    const matches = [...view.state.doc.toString().matchAll(TABLE_ANCHOR_RE)];
    expect(matches).toHaveLength(1);
    expect(readTableModel(entityDoc, matches[0][2])!.rows).toEqual([
      ['h1', 'h2'],
      ['a', 'b'],
    ]);
    expect([...getTablesMap(foreignDoc).keys()]).toHaveLength(0);
    view.destroy();
  });

  it('known entity whose handle has NOT landed → explicit failure, never the slot ydoc', async () => {
    mountHook();

    // The conn-land race: the view is registered for DOC_ID but its handle is not
    // published; the slot holds the other column's live handle. The drop must fail
    // loudly (No silent degradation), not import into the foreign ydoc.
    const foreignDoc = new Y.Doc();
    publishHandle(makeHandle(foreignDoc));

    const view = new EditorView({
      state: EditorState.create({
        doc: 'line one\n',
        extensions: [capturedExt!.fileDropExtension as never],
      }),
      parent: document.body,
    });
    editorViewRef!.current = view;
    setViewEntity(view, DOC_ID);

    const file = new File(['h1,h2\na,b\n'], 'data.csv', { type: 'text/csv' });
    await act(async () => {
      view.contentDOM.dispatchEvent(makeDropEvent(file));
      await new Promise(r => setTimeout(r, 20));
    });

    expect(showToast).toHaveBeenCalledWith('csvImportFailed', 'error');
    expect([...view.state.doc.toString().matchAll(TABLE_ANCHOR_RE)]).toHaveLength(0);
    expect([...getTablesMap(foreignDoc).keys()]).toHaveLength(0);
    view.destroy();
  });
});
