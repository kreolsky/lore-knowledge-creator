/**
 * Unit tests for ContentPickerPopup — the picker contracts that repeatedly
 * regressed: click = immediate context write (add vs remove, ghost deltas for
 * the ghost session) WITHOUT closing, Esc/close-on-outside, is_index filtering,
 * the frozen pre-selected-first order, the all-project references fetch on open
 * (cross-doc refs, truncation notice, toast+fallback on failure).
 *
 * Harness: manual createRoot + act per the repo's component-test pattern
 * (ChatHeader.test.tsx). Sorting/positioning utils stay REAL (they have their
 * own test files); only IO and the context writers are mocked.
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const appState: Record<string, unknown> = {
  documents: [
    { document_id: 'd-idx', title: 'Index Doc', is_index: true },
    { document_id: 'd-zulu', title: 'Zulu', is_index: false },
    { document_id: 'd-alpha', title: 'Alpha', is_index: false },
  ],
  references: [{ reference_id: 'r-store', title: 'Store Ref', document_id: 'd-alpha', media_type: 'text' }],
  currentProject: { project_id: 'p1' },
  showToast: vi.fn(),
};
vi.mock('../../store/app-store', () => ({
  useAppStore: Object.assign((sel: (s: unknown) => unknown) => sel(appState), { getState: () => appState }),
}));

const apiGet = vi.fn();
vi.mock('../../api/client', () => ({ apiClient: { get: (...a: unknown[]) => apiGet(...a) } }));

// vi.hoisted: the vi.mock factory below spreads these at factory time (before
// test-file top-level consts initialize) — a hoisting-safe binding is required.
const contextOps = vi.hoisted(() => ({
  addItemToContext: vi.fn(),
  removeItemFromContext: vi.fn(),
  addGhostDelta: vi.fn(),
  removeGhostDelta: vi.fn(),
}));
vi.mock('../../chat/context', () => ({
  ...contextOps,
  GHOST_SESSION_ID: '__ghost__',
}));

vi.mock('../../api/links', () => ({
  fetchDocumentLinks: vi.fn().mockResolvedValue([]),
  fetchReferenceLinks: vi.fn().mockResolvedValue([]),
  linkCache: new Map<string, unknown>(),
}));
vi.mock('../../hooks/usePopupSlot', () => ({ usePopupSlot: () => true }));
vi.mock('../../hooks/useNavMode', () => ({ useNavMode: () => ({ isKeyboard: false, activateKeyboard: vi.fn() }) }));
vi.mock('../../hooks/useDocumentPreview', () => ({ useDocumentPreview: () => ({ content: undefined, error: null, loading: false }) }));
vi.mock('../../hooks/useReferencePreview', () => ({ useReferencePreview: () => ({ preview: null, error: null, loading: false }) }));
vi.mock('../PickerPreviewPopup', () => ({
  PickerPreviewPopup: () => createElement('div', { 'data-testid': 'picker-preview' }),
}));
vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

import { ContentPickerPopup } from './ContentPickerPopup';

const anchorRect = { left: 40, top: 100, right: 60, bottom: 120, width: 20, height: 20 } as DOMRect;
let onClose: Mock<(reason?: 'esc') => void>;
let root: Root | null = null;

function mountPicker(props: Partial<Parameters<typeof ContentPickerPopup>[0]> = {}) {
  onClose = vi.fn<(reason?: 'esc') => void>();
  const host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(createElement(ContentPickerPopup, {
    sessionId: 's-1',
    selectedDocIds: [],
    selectedRefIds: [],
    onClose,
    anchorRect,
    anchorDocId: 'd-alpha',
    sessionRefId: null,
    ...props,
  })));
  return host;
}
function unmount() {
  // Capture into a local: `root` is a mutable module-level binding, so TS drops
  // the null-narrowing inside the act() closure.
  const r = root;
  if (r) act(() => r.unmount());
  root = null;
}

/** Row titles in rendered order. */
const rowTitles = () =>
  Array.from(document.querySelectorAll('.link-suggest-item')).map((el) => el.querySelector('.truncate')?.textContent);
const clickRow = (title: string) => {
  const row = Array.from(document.querySelectorAll('.link-suggest-item'))
    .find((el) => el.querySelector('.truncate')?.textContent === title);
  if (!row) throw new Error('row not found: ' + title);
  act(() => row.dispatchEvent(new MouseEvent('click', { bubbles: true })));
};

beforeEach(() => {
  vi.clearAllMocks();
  apiGet.mockResolvedValue([]); // /references fetch resolves empty by default
});

describe('ContentPickerPopup', () => {
  it('documents tab hides is_index docs and lists titles', () => {
    mountPicker();
    expect(rowTitles()).toContain('Alpha');
    expect(rowTitles()).toContain('Zulu');
    expect(rowTitles()).not.toContain('Index Doc');
    unmount();
  });

  it('click on an unselected item writes addItemToContext and does NOT close the popup', () => {
    mountPicker();
    clickRow('Zulu');
    expect(contextOps.addItemToContext).toHaveBeenCalledWith('s-1', 'doc', 'd-zulu');
    expect(onClose).not.toHaveBeenCalled();
    expect(document.querySelector('.link-suggest-popup')).not.toBeNull();
    unmount();
  });

  it('click on a selected item removes it from the context', () => {
    mountPicker({ selectedDocIds: ['d-zulu'] });
    clickRow('Zulu');
    expect(contextOps.removeItemFromContext).toHaveBeenCalledWith('s-1', 'doc', 'd-zulu');
    expect(contextOps.addItemToContext).not.toHaveBeenCalled();
    unmount();
  });

  it('a GHOST session folds clicks into ghost deltas, never stored-context writes', () => {
    mountPicker({ sessionId: '__ghost__' });
    clickRow('Alpha');
    expect(contextOps.addGhostDelta).toHaveBeenCalledWith('doc', 'd-alpha');
    unmount();
    mountPicker({ sessionId: '__ghost__', selectedDocIds: ['d-alpha'] });
    clickRow('Alpha');
    expect(contextOps.removeGhostDelta).toHaveBeenCalledWith('doc', 'd-alpha');
    expect(contextOps.addItemToContext).not.toHaveBeenCalled();
    expect(contextOps.removeItemFromContext).not.toHaveBeenCalled();
    unmount();
  });

  it('pre-selected docs render FIRST (frozen at open) regardless of sort order', () => {
    mountPicker({ selectedDocIds: ['d-zulu'] });
    expect(rowTitles()).toEqual(['Zulu', 'Alpha']);
    unmount();
  });

  it('Esc closes with reason esc; mousedown outside closes with no reason', () => {
    mountPicker();
    const input = document.querySelector('.link-suggest-popup input') as HTMLInputElement;
    act(() => input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    expect(onClose).toHaveBeenCalledWith('esc');
    act(() => document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })));
    expect(onClose).toHaveBeenCalledWith();
    unmount();
  });

  it('Cmd+ArrowRight cycles to references and Enter toggles the hovered item', () => {
    mountPicker();
    const input = document.querySelector('.link-suggest-popup input') as HTMLInputElement;
    act(() => input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', metaKey: true, bubbles: true })));
    // References tab: placeholder flips to the references variant.
    expect((document.querySelector('.link-suggest-popup input') as HTMLInputElement).placeholder).toBe('searchReferencesPlaceholder');
    act(() => input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
    expect(contextOps.addItemToContext).toHaveBeenCalledWith('s-1', 'ref', 'r-store');
    unmount();
  });

  it('references tab lists CROSS-DOC refs from the on-open project fetch, not just store scope', async () => {
    apiGet.mockResolvedValue([
      { reference_id: 'r-cross', title: 'Cross Doc Ref', document_id: 'd-zulu', media_type: 'text' },
    ]);
    mountPicker({ initialTab: 'references' });
    await act(async () => { await Promise.resolve(); });
    expect(apiGet).toHaveBeenCalledWith('/references?project_id=p1&limit=1000');
    expect(rowTitles()).toContain('Cross Doc Ref');
    unmount();
  });

  it('refs fetch failure: toast + fallback to the store list (tab stays usable)', async () => {
    apiGet.mockRejectedValue(new Error('net'));
    mountPicker({ initialTab: 'references' });
    await act(async () => { await Promise.resolve(); });
    expect(appState.showToast).toHaveBeenCalledWith('failedToLoadReferences', 'error');
    expect(rowTitles()).toContain('Store Ref');
    unmount();
  });

  it('a response at the 1000-ref cap surfaces the truncation notice', async () => {
    apiGet.mockResolvedValue(
      Array.from({ length: 1000 }, (_, i) => ({ reference_id: `r-${i}`, title: `Ref ${i}`, document_id: 'd-alpha', media_type: 'text' })),
    );
    mountPicker({ initialTab: 'references' });
    await act(async () => { await Promise.resolve(); });
    expect(document.querySelector('.link-suggest-popup')?.textContent).toContain('referencesListTruncated');
    unmount();
  });
});
