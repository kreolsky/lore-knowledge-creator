/** Public notes store: create-with-first-message, the 409 path, the guest name, and which anchors may paint. */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';

const api = vi.hoisted(() => ({
  publicNotesByDoc: vi.fn(),
  publicDocumentByDoc: vi.fn(),
  createPublicNote: vi.fn(),
  replyPublicNote: vi.fn(),
  deletePublicNoteMessage: vi.fn(),
  deletePublicNote: vi.fn(),
}));
vi.mock('../api/public-share', () => api);

import {
  usePublicNotesStore, paintablePublicNoteRanges, publicDocIsBehind, PUBLIC_DRAFT_NOTE_ID,
} from './public-notes-store';
import { useAppStore } from './app-store';
import { HttpError } from '../api/client';
import { t } from '../i18n';
import type { PublicNote } from '../api/public-share';

function note(id: string, anchor: PublicNote['anchor'], mine = true): PublicNote {
  return {
    session_id: id, created_at: null, anchor, anchor_quote: 'мир', is_mine: mine,
    messages: [{
      message_id: `${id}-m1`, parent_id: null, content: 'первое 😀', created_at: null,
      author_name: 'Гость', is_mine: mine,
    }],
  };
}

const showToast = vi.fn();

beforeEach(() => {
  Object.values(api).forEach(fn => fn.mockReset());
  showToast.mockReset();
  useAppStore.setState({ showToast, currentReference: null });
  usePublicNotesStore.getState().reset();
  usePublicNotesStore.getState().setAuthorName('Гость');
  usePublicNotesStore.getState().setServed('doc-1', 'sha-1');
  api.publicNotesByDoc.mockResolvedValue({ allow_comments: true, content_sha256: 'sha-1', notes: [] });
});

describe('public notes store', () => {
  it('every new note bumps draftSeq; carrying the draft across a member edit does not', async () => {
    const seq = usePublicNotesStore.getState().draftSeq;
    usePublicNotesStore.getState().startNote({ selection: { start: 0, end: 1, content_sha256: 'sha-1' }, quote: 'x' });
    expect(usePublicNotesStore.getState().draftSeq).toBe(seq + 1);
    useAppStore.setState({ currentDocument: { document_id: 'doc-1', content: 'xy' } as never });
    api.publicDocumentByDoc.mockResolvedValue({ content: 'xyz', content_sha256: 'sha-2', tables_json: null, headings: [] });
    expect(await usePublicNotesStore.getState().refreshText('doc-1')).toBe(true);
    expect(usePublicNotesStore.getState().draftSeq).toBe(seq + 1);
    useAppStore.setState({ currentDocument: null });
  });

  it('deleting a message that got a reply meanwhile says so, then reloads', async () => {
    api.deletePublicNoteMessage.mockRejectedValue(new HttpError(409, 'Cannot delete a message that has replies'));
    await usePublicNotesStore.getState().deleteMessage('n1', 'm1');
    expect(showToast).toHaveBeenCalledWith(t('publicNoteMessageHasReplies'), 'error');
    expect(api.publicNotesByDoc).toHaveBeenCalledWith('doc-1');
  });

  it('a draft is created together with its first message, then the list reloads and the note opens', async () => {
    api.createPublicNote.mockResolvedValue(note('n1', { state: 'ok', start: 9, end: 12 }));
    const selection = { start: 9, end: 12, content_sha256: 'sha-1' };
    usePublicNotesStore.getState().startNote({ selection, quote: 'мир' });

    expect(await usePublicNotesStore.getState().submit('  текст 😀 ')).toBe(true);

    expect(api.createPublicNote).toHaveBeenCalledWith('doc-1', {
      author_name: 'Гость', content: 'текст 😀', ...selection,
    });
    expect(api.publicNotesByDoc).toHaveBeenCalledWith('doc-1');
    const s = usePublicNotesStore.getState();
    expect(s.draft).toBeNull();
    expect(s.activeNoteId).toBe('n1');
  });

  it('a reply goes under the last message of the open note', async () => {
    usePublicNotesStore.setState({ notes: [note('n1', null)] });
    usePublicNotesStore.getState().openNote('n1');
    api.replyPublicNote.mockResolvedValue({});

    expect(await usePublicNotesStore.getState().submit('ответ')).toBe(true);
    expect(api.replyPublicNote).toHaveBeenCalledWith('doc-1', 'n1', {
      author_name: 'Гость', content: 'ответ', parent_id: 'n1-m1',
    });
  });

  it('a changed document (409) keeps the draft and says to reload', async () => {
    api.createPublicNote.mockRejectedValue(new HttpError(409, 'changed'));
    usePublicNotesStore.getState().startNote({ selection: { start: 0, end: 1, content_sha256: 'old' }, quote: 'x' });

    expect(await usePublicNotesStore.getState().submit('текст')).toBe(false);
    expect(usePublicNotesStore.getState().draft).not.toBeNull();
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast.mock.calls[0][0]).toBe(t('publicNoteDocChanged'));
    expect(showToast.mock.calls[0][1]).toBe('error');
  });

  it('nothing is sent without a name', async () => {
    usePublicNotesStore.getState().setAuthorName('   ');
    usePublicNotesStore.getState().startNote({ selection: null, quote: '' });
    expect(await usePublicNotesStore.getState().submit('текст')).toBe(false);
    expect(api.createPublicNote).not.toHaveBeenCalled();
  });

  it('the guest name is remembered across page loads', () => {
    usePublicNotesStore.getState().setAuthorName('Мария');
    expect(localStorage.getItem('lore.anonName')).toBe('Мария');
  });

  it('a failed list load drops the previous list and reports it', async () => {
    usePublicNotesStore.setState({ notes: [note('n1', null)], notesSha: 'sha-1' });
    api.publicNotesByDoc.mockRejectedValue(new Error('down'));
    await usePublicNotesStore.getState().load('doc-1');
    expect(usePublicNotesStore.getState().notes).toEqual([]);
    expect(showToast).toHaveBeenCalledTimes(1);
  });

  it('paints only `ok` anchors, and only while the list was resolved against the shown text', () => {
    usePublicNotesStore.setState({
      notes: [
        note('ok', { state: 'ok', start: 9, end: 12 }),
        note('orphan', { state: 'orphan', start: null, end: null }),
        note('free', null),
      ],
      notesSha: 'sha-1',
    });
    expect(paintablePublicNoteRanges()).toEqual([{ id: 'ok', from: 9, to: 12 }]);

    usePublicNotesStore.setState({ notesSha: 'sha-newer' });
    expect(paintablePublicNoteRanges()).toEqual([]);
  });

  it('the note being composed is painted over its selection, like a member\'s new note', () => {
    usePublicNotesStore.setState({ notesSha: 'sha-newer' });
    usePublicNotesStore.getState().startNote({
      selection: { start: 9, end: 12, content_sha256: 'sha-1' }, quote: 'мир',
    });
    expect(paintablePublicNoteRanges()).toEqual([{ id: PUBLIC_DRAFT_NOTE_ID, from: 9, to: 12 }]);

    usePublicNotesStore.getState().closeThread();
    expect(paintablePublicNoteRanges()).toEqual([]);
  });

  it('a failed refetch keeps the open thread and its list, marks them stale, and toasts once', async () => {
    api.publicNotesByDoc.mockResolvedValueOnce({ allow_comments: true, content_sha256: 'sha-1', notes: [note('n1', null)] });
    await usePublicNotesStore.getState().load('doc-1');
    usePublicNotesStore.getState().openNote('n1');
    api.publicNotesByDoc.mockRejectedValue(new Error('503'));

    await usePublicNotesStore.getState().load('doc-1');
    await usePublicNotesStore.getState().load('doc-1');

    const s = usePublicNotesStore.getState();
    expect(s.notes.map(n => n.session_id)).toEqual(['n1']);
    expect(s.activeNoteId).toBe('n1');
    expect(s.refreshFailed).toBe(true);
    expect(showToast).toHaveBeenCalledTimes(1);

    api.publicNotesByDoc.mockResolvedValue({ allow_comments: true, content_sha256: 'sha-1', notes: [note('n1', null)] });
    await usePublicNotesStore.getState().load('doc-1');
    expect(usePublicNotesStore.getState().refreshFailed).toBe(false);
  });

  it('a failed FIRST load shows no list and says so', async () => {
    api.publicNotesByDoc.mockRejectedValue(new Error('503'));
    await usePublicNotesStore.getState().load('doc-1');
    const s = usePublicNotesStore.getState();
    expect(s.notes).toEqual([]);
    expect(s.refreshFailed).toBe(false);
    expect(showToast).toHaveBeenCalledWith(t('failedToLoadNotes'), 'error');
  });

  it('the text is behind whenever the list was resolved against another hash — a draft does not hold it back', () => {
    usePublicNotesStore.setState({ notesSha: 'sha-2' });
    expect(publicDocIsBehind()).toBe(true);
    usePublicNotesStore.getState().startNote({ selection: { start: 0, end: 1, content_sha256: 'sha-1' }, quote: 'x' });
    expect(publicDocIsBehind()).toBe(true);
    usePublicNotesStore.setState({ notesSha: 'sha-1' });
    expect(publicDocIsBehind()).toBe(false);
  });

  describe('refreshText — a member edits while the guest writes a note', () => {
    const BEFORE = 'Начало 😀 ключевые слова — конец';
    const sel = (text: string, words: string) => {
      const start = text.indexOf(words);
      return { start, end: start + words.length };
    };

    beforeEach(() => {
      useAppStore.setState({
        currentDocument: { document_id: 'doc-1', content: BEFORE } as never,
        setCurrentDocument: (d: never) => useAppStore.setState({ currentDocument: d }),
      } as never);
      usePublicNotesStore.getState().startNote({
        selection: { ...sel(BEFORE, 'ключевые слова'), content_sha256: 'sha-1' }, quote: 'ключевые слова',
      });
    });

    it('carries the selection over an edit before it, and the page shows the new text', async () => {
      const AFTER = 'Новое вступление ё😀\n' + BEFORE;
      api.publicDocumentByDoc.mockResolvedValue({ content: AFTER, content_sha256: 'sha-2', tables_json: null, headings: [] });

      expect(await usePublicNotesStore.getState().refreshText('doc-1')).toBe(true);

      const s = usePublicNotesStore.getState();
      expect(s.servedSha).toBe('sha-2');
      expect(s.draft?.selection).toEqual({ ...sel(AFTER, 'ключевые слова'), content_sha256: 'sha-2' });
      expect(useAppStore.getState().currentDocument?.content).toBe(AFTER);
      // The draft paints over the same words in the new text.
      expect(paintablePublicNoteRanges()).toEqual([{ id: PUBLIC_DRAFT_NOTE_ID, ...{ from: sel(AFTER, 'ключевые слова').start, to: sel(AFTER, 'ключевые слова').end } }]);
    });

    it('turns the draft into a whole-document note when its words were deleted, and says so', async () => {
      api.publicDocumentByDoc.mockResolvedValue({ content: 'Начало 😀  — конец', content_sha256: 'sha-2', tables_json: null, headings: [] });
      await usePublicNotesStore.getState().refreshText('doc-1');
      expect(usePublicNotesStore.getState().draft?.selection).toBeNull();
      expect(showToast).toHaveBeenCalledWith(t('publicNoteFragmentGone'), 'info');
    });

    it('a 409 on send re-reads the text and sends once more over the moved selection', async () => {
      const AFTER = 'Ж😀 ' + BEFORE;
      api.createPublicNote
        .mockRejectedValueOnce(new HttpError(409, 'changed'))
        .mockResolvedValueOnce(note('n1', null));
      api.publicDocumentByDoc.mockResolvedValue({ content: AFTER, content_sha256: 'sha-2', tables_json: null, headings: [] });

      expect(await usePublicNotesStore.getState().submit('текст')).toBe(true);

      expect(api.createPublicNote).toHaveBeenCalledTimes(2);
      expect(api.createPublicNote).toHaveBeenLastCalledWith('doc-1', {
        author_name: 'Гость', content: 'текст', ...sel(AFTER, 'ключевые слова'), content_sha256: 'sha-2',
      });
      expect(showToast).not.toHaveBeenCalled();
    });
  });
});
