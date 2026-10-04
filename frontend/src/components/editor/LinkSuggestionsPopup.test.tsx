/**
 * LinkSuggestionsPopup ref: mode — bounded server title search.
 *
 * The popup must NOT fetch the capped project-wide reference list on open:
 * ref: suggestions come from GET /references?project_id=…&q=…&limit=50,
 * debounced 200 ms on the ref search text, and the returned page is listed.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, act } from '@testing-library/react';
import { LinkSuggestionsPopup } from './LinkSuggestionsPopup';
import { useAppStore } from '../../store/app-store';
import { useNoteChatStore } from '../../store/note-chat-store';
import { apiClient } from '../../api/client';
import { emit } from '../../events';
import type { Reference } from '../../types';

vi.mock('../../api/client', () => ({
  apiClient: { get: vi.fn() },
}));
vi.mock('../../hooks/useDocumentPreview', () => ({
  useDocumentPreview: vi.fn(() => ({ content: undefined, error: false, loading: false })),
}));
vi.mock('../../hooks/useReferencePreview', () => ({
  useReferencePreview: vi.fn(() => ({ preview: null, error: false, loading: false })),
}));
vi.mock('../../hooks/useDocumentTitle', () => ({
  useDocumentTitle: vi.fn(() => 'T'),
}));
vi.mock('../../editor/active-editor', () => ({
  useEditorView: vi.fn(() => () => null),
}));
vi.mock('../../hooks/useNavMode', () => ({
  useNavMode: vi.fn(() => ({ isKeyboard: false, activateKeyboard: vi.fn() })),
}));
vi.mock('../../hooks/usePopupSlot', () => ({
  usePopupSlot: vi.fn(() => true),
}));

const refItem = (id: string, title: string) => ({
  reference_id: id, title, media_type: 'markdown', is_reference: true, project_id: 'p1',
}) as unknown as Reference;

function openPopup() {
  act(() => {
    emit('show-link-suggestions', {
      pos: 0,
      coords: { top: 100, lineTop: 90, left: 100 },
      editorView: {} as never,
    });
  });
}

function typeInSearch(value: string) {
  const input = document.querySelector('.link-suggest-popup input') as HTMLInputElement;
  expect(input).toBeTruthy();
  act(() => { fireEvent.change(input, { target: { value } }); });
}

describe('LinkSuggestionsPopup — ref: mode bounded q search', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    useAppStore.setState({
      currentProject: { project_id: 'p1', index_doc_id: 'idx' } as never,
      currentDocument: { document_id: 'cur' } as never,
      documents: [],
    });
    useNoteChatStore.setState({ sessions: [] } as never);
    (apiClient.get as ReturnType<typeof vi.fn>).mockReset();
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    render(<LinkSuggestionsPopup />);
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('typing in ref: mode issues GET /references?project_id=…&q=…&limit=50 after the 200 ms debounce', async () => {
    openPopup();
    typeInSearch('ref: Alpha');
    // Debounced: nothing before 200 ms.
    await act(async () => { vi.advanceTimersByTime(150); });
    expect(apiClient.get).not.toHaveBeenCalled();
    await act(async () => { vi.advanceTimersByTime(100); });
    expect(apiClient.get).toHaveBeenCalledTimes(1);
    expect(apiClient.get).toHaveBeenCalledWith('/references?project_id=p1&q=Alpha&limit=50');
  });

  it('lists the returned page and re-queries as the search text changes', async () => {
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([refItem('r1', 'Alpha Note')]);
    openPopup();
    typeInSearch('ref: Alpha');
    await act(async () => { vi.advanceTimersByTime(250); });
    const item = document.querySelector('.link-suggest-item') as HTMLElement;
    expect(item?.textContent).toContain('Alpha Note');

    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    typeInSearch('ref: AlphaX');
    await act(async () => { vi.advanceTimersByTime(250); });
    expect(apiClient.get).toHaveBeenLastCalledWith('/references?project_id=p1&q=AlphaX&limit=50');
    expect(document.querySelector('.link-suggest-item')).toBeNull();
  });

  it('before the debounced response lands, the previous page is filtered by the CURRENT text', async () => {
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([
      refItem('r1', 'Recent One'), refItem('r2', 'Dragon Lair'),
    ]);
    openPopup();
    typeInSearch('ref: ');
    await act(async () => { vi.advanceTimersByTime(250); });
    expect(document.querySelectorAll('.link-suggest-item').length).toBe(2);

    typeInSearch('ref: drag');
    // No timer advance: the q=drag request has not fired yet.
    const labels = [...document.querySelectorAll('.link-suggest-item')].map(e => e.textContent);
    expect(labels.length).toBe(1);
    expect(labels[0]).toContain('Dragon Lair');
  });

  it('an empty ref search fetches the bounded default page (q=)', async () => {
    openPopup();
    typeInSearch('ref: ');
    await act(async () => { vi.advanceTimersByTime(250); });
    expect(apiClient.get).toHaveBeenCalledWith('/references?project_id=p1&q=&limit=50');
  });

  it('doc mode does not fetch references at all', async () => {
    openPopup();
    typeInSearch('Kingdoms');
    await act(async () => { vi.advanceTimersByTime(250); });
    const calls = (apiClient.get as ReturnType<typeof vi.fn>).mock.calls.map(c => c[0]);
    expect(calls.some(c => typeof c === 'string' && c.startsWith('/references'))).toBe(false);
  });
});
