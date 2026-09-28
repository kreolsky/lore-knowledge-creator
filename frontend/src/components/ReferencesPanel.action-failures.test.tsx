/**
 * Dead-button regression: retry-transcription and
 * change-reference-parent failures must surface error toasts. Both catches
 * used to only console.error — the retry button reset nothing and the parent
 * picker closed as if the move had happened.
 *
 * Mounts the REAL panel against real stores on the AUTHED surface (harness per
 * ReferencesPanel.public-scope.test.tsx); RefCard and ParentPickerPopup are
 * mocked down to trigger buttons so the handlers are driven directly.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import type { Document, Project, Reference } from '../types';

const NOW = '2026-01-01T00:00:00Z';

type ShowToast = (message: string, type?: 'error' | 'info' | 'warning', options?: { persistent?: boolean }) => void;

const PROJECT: Project = {
  project_id: 'p1', name: 'P1', status: 'active', project_context: '', index_doc_id: 'root',
  voice_recording_doc_id: null, last_accessed_doc_id: null, owner_id: null, is_public: false,
  my_access: 'full', created_at: NOW,
};

function mkDoc(id: string, parent_id: string | null): Document {
  return {
    document_id: id, project_id: 'p1', parent_id, title: id, content: '', path: '',
    is_index: false, created_at: NOW, updated_at: NOW,
  };
}

const REF: Reference = {
  reference_id: 'ref-1', project_id: 'p1', document_id: 'doc-1', title: 'ref-1',
  media_type: 'audio', source_url: null, content: undefined, processing_status: 'error',
  file_path: null, file_meta: null, created_at: NOW, updated_at: NOW,
};

let container: HTMLDivElement;
let root: Root;
let ReferencesPanel: typeof import('./ReferencesPanel').ReferencesPanel;
let useAppStore: typeof import('../store/app-store').useAppStore;
let loadReferencesMock: ReturnType<typeof vi.fn>;
let postMock: ReturnType<typeof vi.fn>;
let patchMock: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn<ShowToast>>;

beforeEach(async () => {
  vi.resetModules();
  loadReferencesMock = vi.fn().mockResolvedValue([REF]);
  postMock = vi.fn();
  patchMock = vi.fn();
  showToast = vi.fn<ShowToast>();

  // STABLE t: the hydrate effect lists t as a dep — a per-render new t loops it.
  const stableT = (k: string) => k;
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: stableT }) }));

  vi.doMock('../api/references-fetch', () => ({ loadReferences: loadReferencesMock }));
  vi.doMock('../api/client', () => ({
    // agent-config GET resolves falsy (no retry loop); post/patch reject per test.
    apiClient: { get: vi.fn().mockResolvedValue(undefined), post: postMock, patch: patchMock, delete: vi.fn() },
  }));

  // RefCard shrunk to two trigger buttons wired to the panel's handlers.
  vi.doMock('./references/RefCard', () => ({
    RefCard: (props: {
      reference: Reference;
      onRetry: (e: { stopPropagation: () => void }, ref: Reference) => void;
      onChangeParent: (ref: Reference, rect: DOMRect) => void;
    }) => createElement('span', null,
      createElement('button', {
        'data-testid': 'refcard-retry',
        onClick: (e: { stopPropagation: () => void }) => props.onRetry(e, props.reference),
      }, 'retry'),
      createElement('button', {
        'data-testid': 'refcard-change-parent',
        onClick: () => props.onChangeParent(props.reference, new DOMRect(0, 0, 10, 10)),
      }, 'change-parent'),
    ),
  }));
  // ParentPickerPopup shrunk to a button invoking the panel's onMoved callback.
  vi.doMock('./ParentPickerPopup', () => ({
    ParentPickerPopup: (props: { onMoved: (newDocumentId: string | null) => void }) =>
      createElement('button', {
        'data-testid': 'ref-parent-picker-move',
        onClick: () => props.onMoved('doc-2'),
      }, 'move'),
  }));

  ({ ReferencesPanel } = await import('./ReferencesPanel'));
  ({ useAppStore } = await import('../store/app-store'));

  useAppStore.setState({
    showToast,
    currentProject: PROJECT,
    documents: [mkDoc('root', null), mkDoc('doc-1', 'root'), mkDoc('doc-2', 'root')],
    currentDocument: mkDoc('doc-1', 'root'),
    references: [],
    accessLevel: 'full',
  });

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../i18n');
  vi.doUnmock('../api/references-fetch');
  vi.doUnmock('../api/client');
  vi.doUnmock('./references/RefCard');
  vi.doUnmock('./ParentPickerPopup');
});

async function flushEffects() {
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

function clickTestid(id: string) {
  const btn = container.querySelector(`[data-testid="${id}"]`);
  if (!btn) throw new Error(`${id} not rendered`);
  act(() => { btn.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
}

describe('ReferencesPanel action failures — no dead buttons', () => {
  it('a rejecting POST /references/{id}/retry shows the retryTranscriptionFailed error toast', async () => {
    postMock.mockRejectedValue(new Error('boom'));
    act(() => root.render(createElement(ReferencesPanel)));
    await flushEffects();

    await act(async () => { clickTestid('refcard-retry'); });

    expect(showToast).toHaveBeenCalledWith('retryTranscriptionFailed', 'error');
  });

  it('a rejecting PATCH (change parent) shows the changeReferenceParentFailed error toast', async () => {
    patchMock.mockRejectedValue(new Error('boom'));
    act(() => root.render(createElement(ReferencesPanel)));
    await flushEffects();

    await act(async () => { clickTestid('refcard-change-parent'); });
    await act(async () => { clickTestid('ref-parent-picker-move'); });

    expect(showToast).toHaveBeenCalledWith('changeReferenceParentFailed', 'error');
  });
});
