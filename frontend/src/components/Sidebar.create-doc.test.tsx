/**
 * Dead-button regression: the open-create-doc handler
 * (Sidebar create-document) must surface an error toast when POST /documents
 * rejects — the .catch used to only console.error. The success branch is
 * pinned too: the toast addition must NOT swallow the navigate emit nor the
 * setTimeout(emit('breadcrumb-start-rename')) auto-rename path.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// useEvent registrations captured by the mock — tests fire handlers directly.
const registered: Record<string, (payload: never) => void> = {};

let container: HTMLDivElement;
let root: Root;
let Sidebar: typeof import('./Sidebar').Sidebar;
let postMock: ReturnType<typeof vi.fn>;
let emitMock: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  vi.resetModules();
  postMock = vi.fn();
  emitMock = vi.fn();
  showToast = vi.fn();
  for (const k of Object.keys(registered)) delete registered[k];

  vi.doMock('../api/client', () => ({
    apiClient: { get: vi.fn().mockResolvedValue({ documents: [] }), post: postMock, patch: vi.fn(), delete: vi.fn() },
  }));
  vi.doMock('../events', () => ({ emit: emitMock }));
  vi.doMock('../hooks/useEvent', () => ({
    useEvent: (name: string, cb: (payload: never) => void) => { registered[name] = cb; },
  }));
  vi.doMock('../hooks/useDocumentRoute', () => ({
    useDocumentRoute: () => ({ projectId: 'p1', documentId: 'd1' }),
  }));
  vi.doMock('react-router-dom', () => ({ useNavigate: () => vi.fn() }));
  vi.doMock('./DocumentTree', () => ({ DocumentTree: () => null }));
  vi.doMock('./DeleteModal', () => ({ DeleteModal: () => null }));
  vi.doMock('./ParentPickerPopup', () => ({ ParentPickerPopup: () => null }));
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

  const appState = {
    accessLevel: 'full',
    documents: [],
    setDocuments: vi.fn(),
    showToast,
  };
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: typeof appState) => unknown) => selector(appState),
      { getState: () => appState },
    ),
  }));
  const uiState = { isPublicShare: false, collapsedDocIds: [], toggleDocExpanded: vi.fn() };
  vi.doMock('../store/ui-store', () => ({
    useUIStore: Object.assign(
      (selector: (s: typeof uiState) => unknown) => selector(uiState),
      { getState: () => uiState },
    ),
  }));

  ({ Sidebar } = await import('./Sidebar'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../api/client');
  vi.doUnmock('../events');
  vi.doUnmock('../hooks/useEvent');
  vi.doUnmock('../hooks/useDocumentRoute');
  vi.doUnmock('react-router-dom');
  vi.doUnmock('./DocumentTree');
  vi.doUnmock('./DeleteModal');
  vi.doUnmock('./ParentPickerPopup');
  vi.doUnmock('../i18n');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('../store/ui-store');
});

describe('Sidebar create-document (open-create-doc) — no dead button', () => {
  it('a rejecting POST /documents shows the createDocumentFailed error toast and never emits breadcrumb-start-rename', async () => {
    postMock.mockRejectedValue(new Error('boom'));
    act(() => root.render(createElement(Sidebar)));

    await act(async () => { registered['open-create-doc']({ parentId: null } as never); });

    expect(showToast).toHaveBeenCalledWith('createDocumentFailed', 'error');
    expect(emitMock).not.toHaveBeenCalledWith('breadcrumb-start-rename');
  });

  it('success branch: emits navigate-to-document AND (after the auto-rename delay) breadcrumb-start-rename', async () => {
    vi.useFakeTimers();
    try {
      postMock.mockResolvedValue({ document_id: 'new-1', title: '', parent_id: null });
      act(() => root.render(createElement(Sidebar)));

      await act(async () => { registered['open-create-doc']({ parentId: null } as never); });
      expect(emitMock).toHaveBeenCalledWith('navigate-to-document', { documentId: 'new-1' });
      expect(showToast).not.toHaveBeenCalled();

      act(() => { vi.advanceTimersByTime(120); });
      expect(emitMock).toHaveBeenCalledWith('breadcrumb-start-rename');
    } finally {
      vi.useRealTimers();
    }
  });
});
