/**
 * ParentPickerPopup — which parents are offered, and what a refused move tells
 * the user.
 * - The Memory folder and every card under it are never offered (on every call
 *   site, reference mode included); ordinary system folders still are.
 * - A failed PATCH toasts and keeps the popup open; a server refusal with a
 *   detail names that reason, anything else falls back to changeParentFailed.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, forwardRef, act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import type { Document, DocumentTreeNode } from '../types';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let ParentPickerPopup: typeof import('./ParentPickerPopup').ParentPickerPopup;
let HttpErrorCls: new (status: number, detail?: string) => Error;
let patchMock: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn>;
let onClose: ReturnType<typeof vi.fn<() => void>>;

const mk = (id: string, title: string, extra: Partial<Document> = {}): Document => ({
  document_id: id, project_id: 'p', parent_id: null, title,
  content: '', path: '', is_index: false, created_at: '', updated_at: '',
  ...extra,
});

const DOCS: Document[] = [
  mk('sys-root', 'Agent', { is_system: true, system_role: 'system_root' }),
  mk('personas', 'Personas', { is_system: true, system_role: 'system_prompt', parent_id: 'sys-root' }),
  mk('memory', 'Memory', { is_system: true, system_role: 'memory_folder', parent_id: 'sys-root' }),
  mk('card', 'Card', { parent_id: 'memory' }),
  mk('card-child', 'Card child', { parent_id: 'card' }),
  mk('mover', 'Mover'),
  mk('target', 'Target'),
];

beforeEach(async () => {
  vi.resetModules();
  patchMock = vi.fn();
  showToast = vi.fn();
  onClose = vi.fn<() => void>();
  const appState = { documents: DOCS, setDocuments: vi.fn(), showToast };
  vi.doMock('../api/client', () => {
    class HttpError extends Error {
      status: number;
      detail: string;
      constructor(status: number, detail?: string) {
        super(`HTTP ${status}`);
        this.status = status;
        this.detail = detail ?? '';
      }
    }
    return { apiClient: { patch: patchMock, get: vi.fn() }, HttpError };
  });
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: typeof appState) => unknown) => selector(appState),
      { getState: () => appState },
    ),
  }));
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
  vi.doMock('../hooks/useNavMode', () => ({ useNavMode: () => ({ isKeyboard: false, activateKeyboard: vi.fn() }) }));
  vi.doMock('../hooks/useDocumentPreview', () => ({ useDocumentPreview: () => ({ content: undefined, error: false, loading: false }) }));
  vi.doMock('../hooks/usePopupSlot', () => ({ usePopupSlot: () => true }));
  vi.doMock('./PickerPreviewPopup', () => ({ PickerPreviewPopup: () => null }));
  vi.doMock('./ui', () => ({
    FieldInput: forwardRef<HTMLInputElement, Record<string, unknown>>((props, ref) => createElement('input', { ...props, ref } as never)),
  }));
  ({ ParentPickerPopup } = await import('./ParentPickerPopup'));
  ({ HttpError: HttpErrorCls } = await import('../api/client') as unknown as { HttpError: typeof HttpErrorCls });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  for (const m of [
    '../api/client', '../store/app-store', '../i18n', '../hooks/useNavMode',
    '../hooks/useDocumentPreview', '../hooks/usePopupSlot', './PickerPreviewPopup', './ui',
  ]) vi.doUnmock(m);
});

const anchorRect = { top: 0, left: 0, bottom: 10, right: 10, width: 10, height: 10, x: 0, y: 0 } as DOMRect;

function renderFor(doc?: DocumentTreeNode) {
  act(() => {
    root.render(createElement(ParentPickerPopup, doc
      ? { doc, anchorRect, onClose, hidePreview: true }
      : { currentDocumentId: 'mover', onMoved: vi.fn<(id: string | null) => void>(), anchorRect, onClose, hidePreview: true }));
  });
}

const offered = () => Array.from(document.body.querySelectorAll('.link-suggest-item'))
  .map(b => b.textContent ?? '');
const offers = (title: string) => offered().some(label => label.includes(title));
const moverNode = { ...DOCS[5], children: [] } as DocumentTreeNode;

async function pick(title: string) {
  const btn = Array.from(document.body.querySelectorAll<HTMLButtonElement>('.link-suggest-item'))
    .find(b => (b.textContent ?? '').includes(title));
  if (!btn) throw new Error(`${title} not offered`);
  await act(async () => { btn.click(); });
}

describe('ParentPickerPopup — offered parents', () => {
  it('never offers the Memory folder or anything under it', () => {
    renderFor(moverNode);
    expect(offers('Target')).toBe(true);
    expect(offers('Personas')).toBe(true);
    expect(offers('Memory')).toBe(false);
    expect(offers('Card')).toBe(false);
  });

  it('hides memory in reference mode too', () => {
    renderFor();
    expect(offers('Target')).toBe(true);
    expect(offers('Memory')).toBe(false);
    expect(offers('Card')).toBe(false);
  });
});

describe('ParentPickerPopup — a refused move', () => {
  it('toasts the server detail and stays open', async () => {
    patchMock.mockRejectedValue(new HttpErrorCls(400, JSON.stringify({ detail: 'Cannot move a document into its own subtree (cycle)' })));
    renderFor(moverNode);
    await pick('Target');
    expect(showToast).toHaveBeenCalledWith('Cannot move a document into its own subtree (cycle)', 'error');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('falls back to changeParentFailed without a detail', async () => {
    patchMock.mockRejectedValue(new Error('network'));
    renderFor(moverNode);
    await pick('Target');
    expect(showToast).toHaveBeenCalledWith('changeParentFailed', 'error');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on success', async () => {
    patchMock.mockResolvedValue({ ...DOCS[5], parent_id: 'target' });
    renderFor(moverNode);
    await pick('Target');
    expect(showToast).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });
});
