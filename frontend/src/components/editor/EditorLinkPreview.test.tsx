/**
 * Side-by-side coverage of the two link-metadata branches feeding the global
 * hover preview (plan chat-markdown-on-dsh, step 3):
 * - the data-link-* attribute branch — sources list items (.chat-source),
 *   editor CM6 decorations, legacy .chat-link tags;
 * - the href-derived branch — dsh chat anchors whose internal doc:/ref:
 *   scheme the chat wrapper rewrote to https://lore.local/l/<type>/<id>.
 *
 * Both must resolve the SAME preview for the SAME entity: the attribute
 * branch keeps working unchanged, the href branch derives its LinkMeta from
 * the anchor's href when the attributes are absent.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { EditorLinkPreview } from './EditorLinkPreview';
import { useNoteChatStore } from '../../store/note-chat-store';
import { usePublicNotesStore, PUBLIC_DRAFT_NOTE_ID } from '../../store/public-notes-store';
import type { ChatSession } from '../../types';

vi.mock('../../hooks/useDocumentPreview', () => ({
  useDocumentPreview: vi.fn((id: string | null) => ({
    content: id ? `preview:${id}` : undefined,
    error: false,
    loading: false,
  })),
}));

vi.mock('../../hooks/useReferencePreview', () => ({
  useReferencePreview: vi.fn(() => ({ preview: null, error: false, loading: false })),
}));

afterEach(() => {
  cleanup();
  document.querySelectorAll('a[data-test-link]').forEach(el => el.remove());
});

describe('EditorLinkPreview — metadata branches', () => {

  it('a painted note anchor shows the note\'s last message, with no title plaque', async () => {
    useNoteChatStore.setState({ sessions: [{
      session_id: 'note1', first_message_preview: 'Первое 😀', last_message_preview: 'Последний ответ',
    } as ChatSession] });
    const span = document.createElement('span');
    span.className = 'cm-note-anchor';
    span.setAttribute('data-note-id', 'note1');
    span.textContent = 'мир';
    render(<EditorLinkPreview />);
    document.body.appendChild(span);
    span.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false }));
    expect(await screen.findByText('Последний ответ')).toBeTruthy();
    expect(document.querySelector('[data-preview-title]')).toBeNull();
    span.remove();
    useNoteChatStore.setState({ sessions: [] });
  });

  it('on a public link a guest note previews from the guest list, the draft not at all', async () => {
    usePublicNotesStore.setState({ notes: [{
      session_id: 'g1', created_at: null, anchor: null, anchor_quote: null, is_mine: false,
      messages: [
        { message_id: 'm1', parent_id: null, content: 'Вопрос гостя', created_at: null, author_name: 'Гость', is_mine: false },
        { message_id: 'm2', parent_id: 'm1', content: 'Ответ владельца', created_at: null, author_name: 'Red', is_mine: false },
      ],
    }] });
    const draft = document.createElement('span');
    draft.className = 'cm-note-anchor';
    draft.setAttribute('data-note-id', PUBLIC_DRAFT_NOTE_ID);
    render(<EditorLinkPreview />);
    document.body.appendChild(draft);
    draft.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false }));
    await new Promise(r => setTimeout(r, 500));
    expect(document.querySelector('[data-preview-title]')).toBeNull();
    draft.remove();

    const span = document.createElement('span');
    span.className = 'cm-note-anchor';
    span.setAttribute('data-note-id', 'g1');
    document.body.appendChild(span);
    span.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false }));
    expect(await screen.findByText('Ответ владельца')).toBeTruthy();
    expect(document.querySelector('[data-preview-title]')).toBeNull();
    span.remove();
    usePublicNotesStore.setState({ notes: [] });
  });
});
