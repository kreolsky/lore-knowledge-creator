/**
 * Subtree chat privacy — the Access-tab checkbox: reflects the flag of the
 * documents-list row, flips it optimistically, writes back the PATCH value
 * WITHOUT dropping server-derived row fields (key_capabilities, public_share),
 * and on failure restores the prior value and toasts.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, act } from 'react';
import { createRoot, type Root } from 'react-dom/client';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import type { Document } from '../../types';

let container: HTMLDivElement;
let root: Root;
let patchMock: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn>;
let appState: { documents: Document[]; setDocuments: (d: Document[]) => void; accessLevel: string; showToast: typeof showToast };

const row = (id: string, extra: Partial<Document> = {}): Document => ({
  document_id: id, project_id: 'p', parent_id: null, title: id, content: '', path: '',
  is_index: false, created_at: '', updated_at: '', ...extra,
});

const flagOf = (id: string) => appState.documents.find(d => d.document_id === id)!.chats_unindexed;

beforeEach(() => {
  vi.resetModules();
  patchMock = vi.fn();
  showToast = vi.fn();
  appState = {
    documents: [row('doc-1', { key_capabilities: ['agent'], public_share: true }), row('doc-2', { chats_unindexed: true })],
    setDocuments: (d) => { appState.documents = d; },
    accessLevel: 'full',
    showToast,
  };
  vi.doMock('../../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: typeof appState) => unknown) => selector(appState),
      { getState: () => appState },
    ),
  }));
  vi.doMock('../../api/client', () => ({ apiClient: { patch: patchMock } }));
  vi.doMock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
  vi.doMock('../ui', () => ({
    SectionHeader: () => null,
    FieldCheckbox: (p: { checked: boolean; onChange: (v: boolean) => void; label: string }) => createElement('input', {
      type: 'checkbox', 'data-label': p.label, checked: p.checked,
      onChange: (e: { target: { checked: boolean } }) => p.onChange(e.target.checked),
    }),
  }));
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../../store/app-store');
  vi.doUnmock('../../api/client');
  vi.doUnmock('../../i18n');
  vi.doUnmock('../ui');
});

async function render(docId: string) {
  const { ChatsPrivacySection } = await import('./ChatsPrivacySection');
  act(() => root.render(createElement(ChatsPrivacySection, { docId })));
  return container.querySelector<HTMLInputElement>('[data-label="accessChatsPrivateToggle"]')!;
}

async function click(box: HTMLInputElement) {
  await act(async () => { box.click(); });
}

describe('ChatsPrivacySection', () => {
  it('the checkbox reflects the documents-list flag', async () => {
    expect((await render('doc-1')).checked).toBe(false);
    act(() => root.unmount());
    root = createRoot(container);
    expect((await render('doc-2')).checked).toBe(true);
  });

  it('a flip PATCHes the flag and keeps the row\'s server-derived fields', async () => {
    let resolve!: (v: Document) => void;
    patchMock.mockReturnValue(new Promise<Document>((r) => { resolve = r; }));
    const box = await render('doc-1');
    await click(box);
    expect(patchMock).toHaveBeenCalledWith('/documents/doc-1', { chats_unindexed: true });
    expect(flagOf('doc-1')).toBe(true); // optimistic, before the PATCH lands
    // The PATCH response carries no key_capabilities / public_share (only the
    // project GET derives them) — they must survive on the tree row.
    await act(async () => { resolve(row('doc-1', { chats_unindexed: true })); });
    const doc1 = appState.documents.find(d => d.document_id === 'doc-1')!;
    expect(doc1.chats_unindexed).toBe(true);
    expect(doc1.key_capabilities).toEqual(['agent']);
    expect(doc1.public_share).toBe(true);
    expect(showToast).not.toHaveBeenCalled();
  });

  it('a failed PATCH restores the prior value and toasts', async () => {
    patchMock.mockRejectedValue(new Error('boom'));
    const box = await render('doc-2');
    await click(box);
    expect(patchMock).toHaveBeenCalledWith('/documents/doc-2', { chats_unindexed: false });
    expect(flagOf('doc-2')).toBe(true);
    expect(showToast).toHaveBeenCalledWith('docChatsUnindexedFailed', 'error');
  });

  it('a non-editor flip sends nothing (the handler guard, not only the hidden tab)', async () => {
    appState.accessLevel = 'commentator';
    const box = await render('doc-1');
    await click(box);
    expect(patchMock).not.toHaveBeenCalled();
    expect(flagOf('doc-1')).toBeUndefined();
  });
});
