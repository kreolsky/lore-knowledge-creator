/** PublicNotesPanel: which list cards a guest may delete, and where the caret goes on a new note. */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, act } from '@testing-library/react';

const api = vi.hoisted(() => ({
  publicNotesByDoc: vi.fn(),
  publicDocumentByDoc: vi.fn(),
  createPublicNote: vi.fn(),
  replyPublicNote: vi.fn(),
  deletePublicNoteMessage: vi.fn(),
  deletePublicNote: vi.fn(),
}));
vi.mock('../api/public-share', () => api);
vi.mock('./chat/MessageBubble', () => ({ MessageBubble: () => null }));

import { PublicNotesPanel } from './PublicNotesPanel';
import { usePublicNotesStore } from '../store/public-notes-store';
import { useAppStore } from '../store/app-store';
import { t } from '../i18n';
import type { PublicNote, PublicNoteMessage } from '../api/public-share';

function msg(id: string, mine: boolean, parent: string | null = null): PublicNoteMessage {
  return { message_id: id, parent_id: parent, content: `текст ${id}`, created_at: null, author_name: 'Гость', is_mine: mine };
}

function note(id: string, messages: PublicNoteMessage[], mine = true): PublicNote {
  return { session_id: id, created_at: null, anchor: null, anchor_quote: null, is_mine: mine, messages };
}

function cardDelete(id: string): Element | null {
  return document.getElementById(`note-${id}`)?.querySelector(`[title="${t('deleteNote')}"]`) ?? null;
}

beforeEach(() => {
  useAppStore.setState({ showToast: vi.fn(), currentReference: null });
  usePublicNotesStore.getState().reset();
  usePublicNotesStore.getState().setAuthorName('Гость');
  usePublicNotesStore.setState({ documentId: 'doc-1', allowComments: true, loaded: true });
});

afterEach(cleanup);

describe('PublicNotesPanel', () => {
  it('a background refetch of an empty list keeps the empty state instead of flashing the loader', () => {
    usePublicNotesStore.setState({ notes: [], loading: true });
    render(<PublicNotesPanel />);
    expect(document.body.textContent).toContain(t('publicNotesEmpty'));
  });

  it('the first load, before any list arrived, shows the loader', () => {
    usePublicNotesStore.setState({ notes: [], loading: true, loaded: false });
    render(<PublicNotesPanel />);
    expect(document.body.textContent).not.toContain(t('publicNotesEmpty'));
  });

  it('a list card offers delete only for the guest\'s own single-message note', () => {
    usePublicNotesStore.setState({ notes: [
      note('single', [msg('a', true)]),
      note('answered', [msg('b', true), msg('c', false, 'b')]),
      note('own-chain', [msg('d', true), msg('e', true, 'd')]),
      note('foreign', [msg('f', false)], false),
    ] });
    render(<PublicNotesPanel />);
    expect(cardDelete('single')).not.toBeNull();
    expect(cardDelete('answered')).toBeNull();
    expect(cardDelete('own-chain')).toBeNull();
    expect(cardDelete('foreign')).toBeNull();
  });

  it('starting a new note while a thread is open moves the caret to the new composer', () => {
    usePublicNotesStore.setState({ notes: [note('n1', [msg('a', true)])] });
    usePublicNotesStore.getState().openNote('n1');
    render(<PublicNotesPanel />);
    const before = document.querySelector('textarea');
    (document.activeElement as HTMLElement | null)?.blur();

    act(() => usePublicNotesStore.getState().startNote({ selection: { start: 0, end: 1, content_sha256: 's' }, quote: 'x' }));

    const after = document.querySelector('textarea');
    expect(after).not.toBe(before);
    expect(document.activeElement).toBe(after);
  });
});

describe('PublicNotesPanel Escape on a new note', () => {
  const pressEscape = (el: Element) => {
    act(() => { el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })); });
  };
  const startDraft = () => {
    usePublicNotesStore.setState({ notes: [note('n1', [msg('a', true)])] });
    render(<PublicNotesPanel />);
    act(() => usePublicNotesStore.getState().startNote({ selection: { start: 0, end: 1, content_sha256: 's' }, quote: 'x' }));
  };

  it('Escape in the note text cancels the draft and shows the list', () => {
    startDraft();
    pressEscape(document.querySelector('textarea')!);
    expect(usePublicNotesStore.getState().draft).toBeNull();
    expect(document.getElementById('note-n1')).not.toBeNull();
  });

  it('Escape in the guest name field cancels the draft and shows the list', () => {
    usePublicNotesStore.getState().setAuthorName('');
    startDraft();
    pressEscape(document.querySelector(`input[placeholder="${t('publicNoteNamePlaceholder')}"]`)!);
    expect(usePublicNotesStore.getState().draft).toBeNull();
    expect(document.getElementById('note-n1')).not.toBeNull();
  });
});
