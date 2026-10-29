/**
 * View identity through the real document-editor bundle: the `viewEntity` facet
 * buildEditorExtensions provides is what drop and paste resolve their target by.
 *
 * Split view: the focused slot may hold the OTHER column's item/handle, so content
 * dropped or pasted into a view must land in THAT view's entity. The bundle is built
 * once per <Editor> while the view remounts per entity, so the facet must re-read the
 * live entity id at each EditorState.create.
 */
// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { createElement, useRef } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { EditorView } from '@codemirror/view';
import { EditorState, Compartment } from '@codemirror/state';
import * as Y from 'yjs';

const mocks = vi.hoisted(() => {
  const storeState = {
    showToast: vi.fn(),
    accessLevel: 'full' as const,
    documents: [] as Array<Record<string, unknown>>,
    references: [] as Array<Record<string, unknown>>,
    currentProject: null as Record<string, unknown> | null,
    addReference: vi.fn(),
    replaceReference: vi.fn(),
    updateReference: vi.fn(),
  };
  return { upload: vi.fn(), storeState };
});

vi.mock('../../store/app-store', () => ({
  useAppStore: Object.assign(() => undefined, { getState: () => mocks.storeState, subscribe: () => () => {} }),
}));
vi.mock('../../api/client', () => ({
  apiClient: { upload: mocks.upload },
}));
vi.mock('../../i18n', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
  t: (k: string) => k,
}));
vi.mock('mermaid', () => ({
  default: { initialize: vi.fn(), render: vi.fn(async () => ({ svg: '<svg></svg>' })) },
}));

import { buildEditorExtensions, type EditorExtensionDeps } from './editor-extension-bundle';
import { useEditorFileDrop } from './useEditorFileDrop';
import { setEntityHandle } from '../../collab/active-handle-registry';
import { getViewEntity, publishHandle, releaseHandle } from '../../editor/active-editor';
import type { EntityYjsState } from '../../collab/yjs-provider';
import { getTablesMap, readTableModel, TABLE_ANCHOR_RE } from './live-preview/table-block-model';
import type { Document, Reference } from '../../types';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DOC_A = 'doc-entity-a';
const DOC_B = 'doc-entity-b';

let root: Root | null = null;
let fileDrop: ReturnType<typeof useEditorFileDrop> | null = null;
let editorViewRef: { current: EditorView | null } | null = null;
let activeItemRef: { current: Document | Reference | null } | null = null;

/** Mount the real useEditorFileDrop, exposing its extension and refs. */
function mountDropHook(): void {
  const container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  function Harness() {
    const viewRef = useRef<EditorView | null>(null);
    const itemRef = useRef<Document | Reference | null>(null);
    editorViewRef = viewRef;
    activeItemRef = itemRef;
    fileDrop = useEditorFileDrop({
      editorViewRef: viewRef,
      activeItemRef: itemRef,
      getDropGuard: () => ({ isReadonly: false, editable: true }),
    } as unknown as Parameters<typeof useEditorFileDrop>[0]);
    return createElement('div');
  }
  act(() => root!.render(createElement(Harness)));
}

function makeDeps(activeItemIdRef: { current: string | undefined }): EditorExtensionDeps {
  return {
    renderCompartment: new Compartment(),
    rawModeRef: { current: false },
    fileDropExtension: fileDrop!.fileDropExtension,
    yjsCompartment: new Compartment(),
    activeItemRef: { current: null },
    refIsEmptySetterRef: { current: () => {} },
    activeItemIdRef,
    snapshotPreviewRef: { current: null },
    roleRef: { current: 'primary' },
    isReferenceRef: { current: false },
    collabConnectionRef: { current: null },
    regionResolver: () => null,
  };
}

function mountView(doc: string, extensions: ReturnType<typeof buildEditorExtensions>): EditorView {
  const view = new EditorView({
    state: EditorState.create({ doc, extensions }),
    parent: document.body,
  });
  editorViewRef!.current = view;
  return view;
}

function makeDropEvent(file: File): Event {
  const event = new Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', { value: { types: ['Files'], files: [file] }, configurable: true });
  Object.defineProperty(event, 'clientX', { value: 0, configurable: true });
  Object.defineProperty(event, 'clientY', { value: 0, configurable: true });
  return event;
}

function makePasteEvent(html: string, plain: string): Event {
  const data = new Map([['text/html', html], ['text/plain', plain]]);
  const event = new Event('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'clipboardData', {
    value: { setData: (m: string, v: string) => void data.set(m, v), getData: (m: string) => data.get(m) ?? '' },
    configurable: true,
  });
  return event;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.storeState.documents = [];
  mocks.storeState.currentProject = null;
  // jsdom has no layout: no element under the pointer → the drop targets the host view.
  document.elementFromPoint = () => null;
  mountDropHook();
});

afterEach(() => {
  const r = root;
  if (r) act(() => r.unmount());
  root = null;
  releaseHandle();
  setEntityHandle(DOC_B, null);
});

describe('viewEntity on the document editor', () => {
  it('one bundle, two views: each view reads the entity live at ITS creation', () => {
    const activeItemIdRef = { current: DOC_A as string | undefined };
    const extensions = buildEditorExtensions(makeDeps(activeItemIdRef));
    const viewA = mountView('a', extensions);
    activeItemIdRef.current = DOC_B; // entity switch: <CodeMirrorEditor key> remounts
    const viewB = mountView('b', extensions);
    expect(getViewEntity(viewA)).toBe(DOC_A);
    expect(getViewEntity(viewB)).toBe(DOC_B);
    viewA.destroy();
    viewB.destroy();
  });

  it('image drop and table paste land in the VIEW\'S entity, not the focused slot', async () => {
    const entityDoc = new Y.Doc();
    setEntityHandle(DOC_B, { ydoc: entityDoc } as unknown as EntityYjsState);
    const slotDoc = new Y.Doc();
    publishHandle({ ydoc: slotDoc } as unknown as EntityYjsState);
    activeItemRef!.current = { document_id: DOC_A, project_id: 'p1' } as unknown as Document;
    mocks.storeState.documents = [{ document_id: DOC_A, project_id: 'p1' }, { document_id: DOC_B, project_id: 'p1' }];
    mocks.storeState.currentProject = { project_id: 'p1' };

    const view = mountView('hello', buildEditorExtensions(makeDeps({ current: DOC_B })));

    mocks.upload.mockResolvedValueOnce({ reference_id: 'ref-up-1', media_type: 'image' });
    await act(async () => {
      view.contentDOM.dispatchEvent(makeDropEvent(new File(['x'], 'photo.png', { type: 'image/png' })));
      await new Promise(r => setTimeout(r, 20));
    });
    expect((mocks.upload.mock.calls[0][1] as FormData).get('document_id')).toBe(DOC_B);
    expect(view.state.doc.toString()).toContain('![photo.png](ref:ref-up-1)');

    view.dispatch({ selection: { anchor: view.state.doc.length } });
    view.contentDOM.dispatchEvent(makePasteEvent(
      '<!--lore-clipboard--><table></table>',
      '| A | B |\n| --- | --- |\n| 1 | 2 |',
    ));
    const anchors = [...view.state.doc.toString().matchAll(TABLE_ANCHOR_RE)];
    expect(anchors).toHaveLength(1);
    expect(readTableModel(entityDoc, anchors[0][2])!.rows).toEqual([['A', 'B'], ['1', '2']]);
    expect([...getTablesMap(slotDoc).keys()]).toHaveLength(0);
    view.destroy();
  });
});
