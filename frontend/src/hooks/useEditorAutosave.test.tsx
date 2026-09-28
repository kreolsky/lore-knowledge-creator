/**
 * Contract tests for useEditorAutosave — checkpoint lifecycle over content-sync.
 *
 * Pins the single-write-door ruling: closing the tab (beforeunload) sends NO
 * REST content PATCH (the keepalive door is deleted — the IndexedDB mirror and
 * the yjs resync cover unflushed edits), and a checkpoint whose flushAndWait
 * fails toasts instead of falling back to REST.
 *
 * Minimal renderHook via React.createElement + createRoot (no @testing-library/react),
 * mirroring useEditorCollab.test.ts. Collab dependencies are mocked.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, type RefObject } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import type { EditorView } from '@codemirror/view';

// The custom renderHook (no @testing-library/react) doesn't set the act environment flag,
// which spams "not configured to support act(...)" warnings despite correct act() usage.
// Opt in explicitly so the test output stays clean.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// ── Mocks ────────────────────────────────────────────────────────────────────

const patchMock = vi.fn().mockResolvedValue(undefined);
vi.mock('../api/client', () => ({
  apiClient: { patch: (...args: unknown[]) => patchMock(...args) },
  HttpError: class HttpError extends Error {
    status = 0;
  },
}));

vi.mock('../i18n', () => ({
  t: (key: string) => key,
  useTranslation: () => ({ t: (key: string) => key }),
}));

// The store surface content-sync touches (syncToStore writes + persist toast).
// accessLevel 'full' + the doc in `documents` mean a resurrected REST fallback
// WOULD pass its write guards — the no-PATCH assertions stay falsifiable.
const storeState = {
  accessLevel: 'full' as const,
  documents: [{ document_id: 'doc-1' }],
  references: [] as unknown[],
  currentDocument: null,
  currentReference: null,
  updateReference: vi.fn(),
  setDocuments: vi.fn(),
  showToast: vi.fn(),
};
vi.mock('../store/app-store', () => ({
  useAppStore: { getState: () => storeState, setState: vi.fn() },
}));

// The collab provider the hook reads from the project context; reassigned per test.
let provider: { flushAndWait: ReturnType<typeof vi.fn>; send: ReturnType<typeof vi.fn> };
vi.mock('../collab/ProjectCollabContext', () => ({
  useProjectCollab: () => provider,
}));

import { useEditorAutosave } from './useEditorAutosave';
import { clearAllCheckpointDedup } from '../editor/content-sync';
import type { Document } from '../types';

// ── Fixtures ─────────────────────────────────────────────────────────────────

const doc: Document = {
  document_id: 'doc-1', project_id: 'p1', parent_id: null, title: 'T',
  content: 'x', path: 't.md', is_index: false,
  created_at: '', updated_at: '',
} as Document;

const viewRef = {
  current: { state: { doc: { toString: () => 'typed text' } } },
} as unknown as RefObject<EditorView | null>;

// ── Harness ──────────────────────────────────────────────────────────────────

let container: HTMLDivElement;
let root: Root;

/** Minimal renderHook: mounts the hook in a Probe component, returns its result. */
function renderAutosave(): ReturnType<typeof useEditorAutosave> {
  let ret: ReturnType<typeof useEditorAutosave> | null = null;
  function Probe() {
    ret = useEditorAutosave({
      editorViewRef: viewRef,
      getCollabStatus: () => 'connected' as const,
    });
    return null;
  }
  act(() => { root.render(createElement(Probe)); });
  return ret!;
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  provider = { flushAndWait: vi.fn().mockResolvedValue(undefined), send: vi.fn() };
  patchMock.mockClear();
  storeState.showToast.mockClear();
  clearAllCheckpointDedup();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

// ── beforeunload: no keepalive PATCH ─────────────────────────────────────────

describe('beforeunload sends no content PATCH', () => {
  it('closing the tab triggers no fetch and no apiClient.patch', () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true }) as unknown as typeof fetch;
    vi.stubGlobal('fetch', fetchMock);

    renderAutosave();
    window.dispatchEvent(new Event('beforeunload'));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(patchMock).not.toHaveBeenCalled();
  });
});

// ── checkpoint: flushAndWait failure toasts instead of REST ──────────────────

describe('checkpoint with a failing flushAndWait', () => {
  it('toasts failedToSaveDocument and sends no PATCH', async () => {
    const fetchMock = vi.fn() as unknown as typeof fetch;
    vi.stubGlobal('fetch', fetchMock);
    provider.flushAndWait.mockRejectedValue(new Error('flush down'));

    const h = renderAutosave();
    await expect(h.checkpoint(doc)).rejects.toThrow('flush down');

    expect(storeState.showToast).toHaveBeenCalledWith('failedToSaveDocument', 'error');
    expect(patchMock).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('checkpoint with a healthy flushAndWait', () => {
  it('flushes the entity and resolves without any REST write', async () => {
    const h = renderAutosave();

    await expect(h.checkpoint(doc)).resolves.toBeUndefined();

    expect(provider.flushAndWait).toHaveBeenCalledWith('doc-1');
    expect(patchMock).not.toHaveBeenCalled();
  });
});
