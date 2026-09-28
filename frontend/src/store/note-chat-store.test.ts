/** Tests for note-chat-store action helpers. */
// @vitest-environment jsdom

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { useNoteChatStore, clearNoteSessionsCache } from './note-chat-store';
import { useAppStore } from './app-store';
import { clearUserScopedCaches } from './logout-handlers';
import type { ChatMessage, ChatSession } from '../types';

vi.mock('../api/client', () => ({
  apiClient: {
    post: vi.fn(),
    get: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

import { apiClient } from '../api/client';

beforeEach(() => {
  useNoteChatStore.setState({
    sessions: [],
    activeSessionId: null,
    messages: [],
    messagesLoading: false,
    pendingInputFocus: false,
    pendingImages: [],
    sessionsScope: null,
    lastRequestedSessionsScope: null,
  });
  (apiClient.post as ReturnType<typeof vi.fn>).mockReset();
});

describe('editMessage', () => {
  // WHY: editing a note message must keep the owning session's list-card preview fresh
  // without a full loadSessions refetch. The card reads first_message_preview, so
  // editMessage must re-derive first/last preview from the updated messages array.
  function seedThread() {
    const session = {
      session_id: 'ns1',
      project_id: 'p1',
      document_id: 'd1',
      reference_id: null,
      is_note: true,
      anchor_offset_start: null,
      anchor_offset_end: null,
      title: '',
      mode: 'chat',
      model: '',
      updated_at: '2026-05-21T10:00:00Z',
      created_at: '2026-05-21T10:00:00Z',
      first_message_preview: 'original first',
      last_message_preview: 'original last',
      message_count: 2,
      last_message_at: '2026-05-21T10:01:00Z',
    };
    useNoteChatStore.setState({
      sessions: [session as never],
      activeSessionId: 'ns1',
      messages: [
        { message_id: 'm1', session_id: 'ns1', content: 'original first', created_at: '2026-05-21T10:00:00Z' },
        { message_id: 'm2', session_id: 'ns1', content: 'original last', created_at: '2026-05-21T10:01:00Z' },
      ] as never,
    });
  }

  it('re-derives first_message_preview when the first message is edited', async () => {
    seedThread();
    const updated = {
      message_id: 'm1',
      session_id: 'ns1',
      content: 'EDITED first',
      created_at: '2026-05-21T10:00:00Z',
    };
    (apiClient.patch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(updated);

    await useNoteChatStore.getState().editMessage('m1', 'EDITED first');

    const s = useNoteChatStore.getState();
    expect(s.messages[0].content).toBe('EDITED first');
    expect(s.sessions[0].first_message_preview).toBe('EDITED first');
    // last preview unchanged (last message wasn't edited)
    expect(s.sessions[0].last_message_preview).toBe('original last');
  });

  it('re-derives last_message_preview when the last message is edited', async () => {
    seedThread();
    const updated = {
      message_id: 'm2',
      session_id: 'ns1',
      content: 'EDITED last',
      created_at: '2026-05-21T10:01:00Z',
    };
    (apiClient.patch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(updated);

    await useNoteChatStore.getState().editMessage('m2', 'EDITED last');

    const s = useNoteChatStore.getState();
    expect(s.sessions[0].first_message_preview).toBe('original first');
    expect(s.sessions[0].last_message_preview).toBe('EDITED last');
  });

  it('updates both previews when the single (only) message is edited', async () => {
    useNoteChatStore.setState({
      sessions: [{
        session_id: 'ns1', project_id: 'p1', document_id: 'd1', reference_id: null,
        is_note: true, title: '', mode: 'chat', model: '',
        updated_at: '2026-05-21T10:00:00Z', created_at: '2026-05-21T10:00:00Z',
        first_message_preview: 'only', last_message_preview: 'only',
        message_count: 1, last_message_at: '2026-05-21T10:00:00Z',
      } as never],
      activeSessionId: 'ns1',
      messages: [{ message_id: 'm1', session_id: 'ns1', content: 'only', created_at: '2026-05-21T10:00:00Z' }] as never,
    });
    const updated = { message_id: 'm1', session_id: 'ns1', content: 'EDITED only', created_at: '2026-05-21T10:00:00Z' };
    (apiClient.patch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(updated);

    await useNoteChatStore.getState().editMessage('m1', 'EDITED only');

    const s = useNoteChatStore.getState();
    expect(s.sessions[0].first_message_preview).toBe('EDITED only');
    expect(s.sessions[0].last_message_preview).toBe('EDITED only');
  });

  it('leaves other sessions untouched', async () => {
    seedThread();
    useNoteChatStore.setState(s => ({
      sessions: [...s.sessions, {
        session_id: 'other', project_id: 'p1', document_id: 'd1', reference_id: null,
        is_note: true, title: '', mode: 'chat', model: '',
        updated_at: '', created_at: '',
        first_message_preview: 'other first', last_message_preview: 'other last',
        message_count: 1, last_message_at: '',
      } as never],
    }));
    const updated = { message_id: 'm1', session_id: 'ns1', content: 'EDITED first', created_at: '' };
    (apiClient.patch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(updated);

    await useNoteChatStore.getState().editMessage('m1', 'EDITED first');

    const other = useNoteChatStore.getState().sessions.find(ss => ss.session_id === 'other')!;
    expect(other.first_message_preview).toBe('other first');
    expect(other.last_message_preview).toBe('other last');
  });
});

describe('createNoteSession', () => {
  it('anchorless, no reference — posts is_note=true without anchor fields and prepends', async () => {
    const newSession = {
      session_id: 'ns1',
      project_id: 'p1',
      document_id: 'd1',
      reference_id: null,
      is_note: true,
      anchor_offset_start: null,
      anchor_offset_end: null,
      title: '',
      mode: 'chat',
      model: '',
      updated_at: '2026-05-21T10:00:00Z',
      created_at: '2026-05-21T10:00:00Z',
    };
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValueOnce(newSession);

    const existing = { ...newSession, session_id: 'old', title: 'older' } as never;
    useNoteChatStore.setState({ sessions: [existing] });

    const result = await useNoteChatStore.getState().createNoteSession({ projectId: 'p1', documentId: 'd1' });

    expect(apiClient.post).toHaveBeenCalledWith('/chat/sessions', {
      project_id: 'p1',
      document_id: 'd1',
      is_note: true,
    });
    expect(result).toEqual(newSession);

    const state = useNoteChatStore.getState();
    expect(state.sessions).toHaveLength(2);
    expect(state.sessions[0].session_id).toBe('ns1');
    expect(state.sessions[1].session_id).toBe('old');
    expect(state.activeSessionId).toBe('ns1');
    expect(state.messages).toEqual([]);
    expect(state.messagesLoading).toBe(false);
  });

  // REGRESSION GUARD (this fix's bug): an anchorless note created while a reference
  // is open must attach to the reference (document_id === referenceId), NOT the parent
  // document. Before the unified method this was the createAnchorlessSession bug.
  it('anchorless WITH reference — document_id === referenceId', async () => {
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      session_id: 'ns1', project_id: 'p1', document_id: 'ref1', reference_id: 'ref1',
      is_note: true, anchor_offset_start: null, anchor_offset_end: null,
      title: '', mode: 'chat', model: '', updated_at: '', created_at: '',
    });
    await useNoteChatStore.getState().createNoteSession({
      projectId: 'p1',
      documentId: 'd1',
      referenceId: 'ref1',
    });
    expect(apiClient.post).toHaveBeenCalledWith('/chat/sessions', {
      project_id: 'p1',
      document_id: 'ref1',
      is_note: true,
    });
  });

  it('anchored with reference + rel positions — body includes anchor_* and document_id === referenceId', async () => {
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      session_id: 'ns1', project_id: 'p1', document_id: 'ref1', reference_id: 'ref1',
      is_note: true, anchor_offset_start: 10, anchor_offset_end: 20,
      title: '', mode: 'chat', model: '', updated_at: '', created_at: '',
    });
    await useNoteChatStore.getState().createNoteSession({
      projectId: 'p1',
      documentId: 'd1',
      referenceId: 'ref1',
      anchor: { offsetStart: 10, offsetEnd: 20, relStart: '{"r":1}', relEnd: '{"r":2}' },
    });
    expect(apiClient.post).toHaveBeenCalledWith('/chat/sessions', {
      project_id: 'p1',
      document_id: 'ref1',
      is_note: true,
      anchor_offset_start: 10,
      anchor_offset_end: 20,
      anchor_rel_start: '{"r":1}',
      anchor_rel_end: '{"r":2}',
    });
  });

  it('anchorless body has no anchor_offset_* fields', async () => {
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      session_id: 'ns1', project_id: 'p1', document_id: 'd1', reference_id: null,
      is_note: true, anchor_offset_start: null, anchor_offset_end: null,
      title: '', mode: 'chat', model: '', updated_at: '', created_at: '',
    });
    await useNoteChatStore.getState().createNoteSession({ projectId: 'p1', documentId: 'd1' });
    const body = (apiClient.post as ReturnType<typeof vi.fn>).mock.calls[0][1] as Record<string, unknown>;
    expect(body).not.toHaveProperty('anchor_offset_start');
    expect(body).not.toHaveProperty('anchor_offset_end');
  });

  it('dedups when the realtime frame already added the session (race-safe)', async () => {
    // Regression for the "two identical notes" bug: the realtime note_session_created
    // frame can land BEFORE createNoteSession's fetch resolves (WS onmessage races the
    // fetch promise). upsertSession adds it first, then createNoteSession's commit must
    // REPLACE — not prepend a second copy with the same session_id.
    const session = {
      session_id: 'ns1', project_id: 'p1', document_id: 'd1', reference_id: null,
      is_note: true, anchor_offset_start: null, anchor_offset_end: null,
      title: '', mode: 'chat', model: '', updated_at: '2026-07-01T10:00:00Z', created_at: '2026-07-01T10:00:00Z',
    };
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValueOnce(session);
    // Simulate the realtime frame winning the race.
    useNoteChatStore.getState().upsertSession(session as never);
    expect(useNoteChatStore.getState().sessions).toHaveLength(1);

    await useNoteChatStore.getState().createNoteSession({ projectId: 'p1', documentId: 'd1' });

    const state = useNoteChatStore.getState();
    expect(state.sessions).toHaveLength(1);
    expect(state.sessions[0].session_id).toBe('ns1');
    expect(state.activeSessionId).toBe('ns1');
  });

  it('TOCTOU: a POST resolving after a soft logout does not write into the reset store', async () => {
    // Write-path race: an in-flight createNoteSession POST resolving after logout must
    // not insert the prior user's freshly-created note into the reset store.
    let resolvePost!: (v: unknown) => void;
    (apiClient.post as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise(r => { resolvePost = r; }),
    );
    useNoteChatStore.setState({ sessions: [], activeSessionId: null, messages: [] });

    const p = useNoteChatStore.getState().createNoteSession({ projectId: 'p1', documentId: 'd1' });
    await new Promise(r => setTimeout(r, 0));

    clearUserScopedCaches(); // soft logout mid-POST: bumps the epoch + resets the store
    expect(useNoteChatStore.getState().sessions).toEqual([]);

    resolvePost({
      session_id: 'A-secret', project_id: 'p1', document_id: 'd1', is_note: true,
      title: '', mode: 'chat', model: '', updated_at: '', created_at: '',
    });
    const result = await p;

    expect((result as { session_id: string }).session_id).toBe('A-secret'); // still returned
    expect(useNoteChatStore.getState().sessions).toEqual([]);                // NOT re-inserted
    expect(useNoteChatStore.getState().activeSessionId).toBeNull();
  });
});

// ── note-realtime idempotent remote-apply ──

describe('sendMessage (realtime race dedup)', () => {
  it('does not duplicate when the realtime frame landed before the optimistic commit', async () => {
    useNoteChatStore.setState({
      sessions: [{
        session_id: 'ns1', project_id: 'p1', document_id: 'd1', reference_id: null,
        is_note: true, title: '', mode: 'chat', model: '',
        updated_at: '2026-05-21T10:00:00Z', created_at: '2026-05-21T10:00:00Z',
        first_message_preview: null, last_message_preview: null,
        message_count: 0, last_message_at: null,
      } as never],
      activeSessionId: 'ns1',
      messages: [] as never,
    });
    const serverMsg: ChatMessage = {
      message_id: 'm1', chat_id: 'ns1', parent_id: null, role: 'user',
      content: 'hello', created_at: '2026-05-21T10:00:00Z',
    };
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValueOnce(serverMsg);

    // Simulate the realtime `note_message_changed(added)` frame winning the race.
    useNoteChatStore.getState().applyMessageChange({
      action: 'added', session_id: 'ns1', message: serverMsg,
      preview: { first: 'hello', last: 'hello', count: 1, last_at: '2026-05-21T10:00:00Z' },
    });
    expect(useNoteChatStore.getState().messages).toHaveLength(1);

    await useNoteChatStore.getState().sendMessage('hello');

    const s = useNoteChatStore.getState();
    // No duplicate message; count derived absolutely (1, not 2).
    expect(s.messages).toHaveLength(1);
    expect(s.messages[0].message_id).toBe('m1');
    expect(s.sessions[0].message_count).toBe(1);
    expect(s.sessions[0].last_message_preview).toBe('hello');
  });
});

describe('applyMessageChange', () => {
  function seedOpenThread() {
    useNoteChatStore.setState({
      sessions: [{
        session_id: 'ns1', project_id: 'p1', document_id: 'd1', reference_id: null,
        is_note: true, title: '', mode: 'chat', model: '',
        updated_at: '2026-05-21T10:00:00Z', created_at: '2026-05-21T10:00:00Z',
        first_message_preview: 'first', last_message_preview: 'first',
        message_count: 1, last_message_at: '2026-05-21T10:00:00Z',
      } as never],
      activeSessionId: 'ns1',
      messages: [
        { message_id: 'm1', chat_id: 'ns1', parent_id: null, role: 'user', content: 'first', created_at: '2026-05-21T10:00:00Z' },
      ] as never,
    });
  }

  it('is idempotent — applying the same "added" frame twice does not double-count', () => {
    seedOpenThread();
    const newMsg: ChatMessage = { message_id: 'm2', chat_id: 'ns1', parent_id: 'm1', role: 'user', content: 'second', created_at: '2026-05-21T10:01:00Z' };
    const frame = {
      action: 'added', session_id: 'ns1', message: newMsg,
      preview: { first: 'first', last: 'second', count: 2, last_at: '2026-05-21T10:01:00Z' },
    };
    useNoteChatStore.getState().applyMessageChange(frame);
    useNoteChatStore.getState().applyMessageChange(frame); // duplicate broadcast

    const s = useNoteChatStore.getState();
    expect(s.messages).toHaveLength(2);
    expect(s.messages.map(m => m.message_id)).toEqual(['m1', 'm2']);
    expect(s.sessions[0].message_count).toBe(2);
    expect(s.sessions[0].last_message_preview).toBe('second');
  });

  it('patches the owning session preview even when the thread is not open', () => {
    seedOpenThread();
    useNoteChatStore.setState({ activeSessionId: null });
    useNoteChatStore.getState().applyMessageChange({
      action: 'added', session_id: 'ns1',
      message: { message_id: 'mx', chat_id: 'ns1', parent_id: null, role: 'user', content: 'x', created_at: '' },
      preview: { first: 'first', last: 'remote', count: 2, last_at: '2026-05-21T10:02:00Z' },
    });
    const s = useNoteChatStore.getState();
    // messages untouched (thread not open)
    expect(s.messages).toHaveLength(1);
    // preview patched on the list card
    expect(s.sessions[0].message_count).toBe(2);
    expect(s.sessions[0].last_message_preview).toBe('remote');
  });

  it('removes messages on "deleted" by message_ids', () => {
    seedOpenThread();
    useNoteChatStore.getState().applyMessageChange({
      action: 'added', session_id: 'ns1',
      message: { message_id: 'm2', chat_id: 'ns1', parent_id: 'm1', role: 'user', content: 'second', created_at: '2026-05-21T10:01:00Z' },
      preview: { count: 2, last: 'second', first: 'first', last_at: '2026-05-21T10:01:00Z' },
    });
    useNoteChatStore.getState().applyMessageChange({
      action: 'deleted', session_id: 'ns1', message_ids: ['m2'],
      preview: { count: 1, last: 'first', first: 'first', last_at: '2026-05-21T10:00:00Z' },
    });
    const s = useNoteChatStore.getState();
    expect(s.messages.map(m => m.message_id)).toEqual(['m1']);
    expect(s.sessions[0].message_count).toBe(1);
  });
});

describe('deleteMessage (M1)', () => {
  it('re-derives the owning session preview after a delete', async () => {
    useNoteChatStore.setState({
      sessions: [{
        session_id: 'ns1', project_id: 'p1', document_id: 'd1', reference_id: null,
        is_note: true, title: '', mode: 'chat', model: '',
        updated_at: '2026-05-21T10:00:00Z', created_at: '2026-05-21T10:00:00Z',
        first_message_preview: 'first', last_message_preview: 'second',
        message_count: 2, last_message_at: '2026-05-21T10:01:00Z',
      } as never],
      activeSessionId: 'ns1',
      messages: [
        { message_id: 'm1', chat_id: 'ns1', parent_id: null, role: 'user', content: 'first', created_at: '2026-05-21T10:00:00Z' },
        { message_id: 'm2', chat_id: 'ns1', parent_id: 'm1', role: 'user', content: 'second', created_at: '2026-05-21T10:01:00Z' },
      ] as never,
    });
    (apiClient.delete as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ success: true });

    await useNoteChatStore.getState().deleteMessage('m2');

    const s = useNoteChatStore.getState();
    expect(s.messages).toHaveLength(1);
    expect(s.sessions[0].first_message_preview).toBe('first');
    expect(s.sessions[0].last_message_preview).toBe('first');
    expect(s.sessions[0].message_count).toBe(1);
  });
});

// ── per-session composer drafts (plan chat-draft-persistence) ──
// A note draft is thread-scoped: a shared draft would leak text between distinct
// note threads. Lifecycle: set on type, dropped with the session (delete,
// realtime remove, reset).

describe('drafts (per-session composer drafts)', () => {
  function seedNoteSession(id: string) {
    return {
      session_id: id, project_id: 'p1', document_id: 'd1', reference_id: null,
      is_note: true, title: '', mode: 'chat', model: '',
      updated_at: '2026-05-21T10:00:00Z', created_at: '2026-05-21T10:00:00Z',
    } as never;
  }

  it('setDraftForSession writes only its own session entry', () => {
    useNoteChatStore.getState().setDraftForSession('ns1', 'hello');
    useNoteChatStore.getState().setDraftForSession('ns2', 'other');
    const s = useNoteChatStore.getState();
    expect(s.drafts.ns1).toBe('hello');
    expect(s.drafts.ns2).toBe('other');
  });

  it('deleteSession drops the draft entry (happy path)', async () => {
    useNoteChatStore.setState({
      sessions: [seedNoteSession('ns1')],
      activeSessionId: 'ns1',
      drafts: { ns1: 'draft text' },
    });
    (apiClient.delete as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ success: true });

    await useNoteChatStore.getState().deleteSession('ns1');

    expect(useNoteChatStore.getState().drafts.ns1).toBeUndefined();
  });

  it('a failed deleteSession restores the draft entry (snapshot must capture drafts)', async () => {
    useNoteChatStore.setState({
      sessions: [seedNoteSession('ns1')],
      activeSessionId: 'ns1',
      drafts: { ns1: 'draft text' },
    });
    (apiClient.delete as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('boom'));

    await useNoteChatStore.getState().deleteSession('ns1');

    expect(useNoteChatStore.getState().drafts.ns1).toBe('draft text');
  });

  it('removeSession (realtime) drops the draft entry', () => {
    useNoteChatStore.setState({
      sessions: [seedNoteSession('ns1')],
      drafts: { ns1: 'x' },
    });

    useNoteChatStore.getState().removeSession('ns1');

    expect(useNoteChatStore.getState().drafts.ns1).toBeUndefined();
  });

  it('reset() clears the whole drafts map', () => {
    useNoteChatStore.setState({ drafts: { ns1: 'x' } });

    useNoteChatStore.getState().reset();

    expect(useNoteChatStore.getState().drafts).toEqual({});
  });
});

describe('removeSession', () => {
  it('clears active thread state when the removed session was open', () => {
    useNoteChatStore.setState({
      sessions: [{ session_id: 'ns1', project_id: 'p1', document_id: 'd1', reference_id: null, is_note: true, title: '', mode: 'chat', model: '', updated_at: '', created_at: '' } as never],
      activeSessionId: 'ns1',
      messages: [{ message_id: 'm1', content: 'x' } as never],
    });
    useNoteChatStore.getState().removeSession('ns1');
    const s = useNoteChatStore.getState();
    expect(s.sessions).toHaveLength(0);
    expect(s.activeSessionId).toBeNull();
    expect(s.messages).toEqual([]);
  });

  it('leaves the open thread untouched when removing a different session', () => {
    useNoteChatStore.setState({
      sessions: [
        { session_id: 'ns1', project_id: 'p1', document_id: 'd1', reference_id: null, is_note: true, title: '', mode: 'chat', model: '', updated_at: '', created_at: '' } as never,
        { session_id: 'ns2', project_id: 'p1', document_id: 'd1', reference_id: null, is_note: true, title: '', mode: 'chat', model: '', updated_at: '', created_at: '' } as never,
      ],
      activeSessionId: 'ns1',
      messages: [{ message_id: 'm1', content: 'x' } as never],
    });
    useNoteChatStore.getState().removeSession('ns2');
    const s = useNoteChatStore.getState();
    expect(s.sessions.map(ss => ss.session_id)).toEqual(['ns1']);
    expect(s.activeSessionId).toBe('ns1');
    expect(s.messages).toHaveLength(1);
  });
});

// ── scope reconciliation (sessionsScope + ensureSessions) ──
// The store records WHICH scope its `sessions` hold (the same key the SWR cache
// uses). Why: the pre-commit nav path (Header.setCurrentDocument BEFORE navigate)
// lands on DocumentPage's already-committed guard branch, which skips the whole
// open bundle including its loadSessions — the skip must self-reconcile without
// re-fetching on a same-doc remount, a scope transition must drop the previous
// doc's rows synchronously (NotesPanel only gates the EMPTY list behind the
// spinner), and a superseded fetch must never repaint its scope over the
// now-open one (modeled on chat-store's stale-scope race guard).
describe('sessions scope reconciliation', () => {
  const noteSession = (id: string) =>
    ({
      session_id: id, project_id: 'p1', document_id: 'd1', reference_id: null,
      is_note: true, title: '', mode: 'chat', model: '',
      updated_at: '2026-09-03T10:00:00Z', created_at: '2026-09-03T10:00:00Z',
    } as never);
  const ids = (s: { sessions: ChatSession[] }) => s.sessions.map(x => x.session_id);
  const deferredGet = () => {
    let release!: (v: unknown) => void;
    const pending = new Promise<unknown>(r => { release = r; });
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation(() => pending);
    return release;
  };

  beforeEach(() => {
    clearNoteSessionsCache();
    (apiClient.get as ReturnType<typeof vi.fn>).mockReset();
    useNoteChatStore.getState().reset();
  });

  it('loadSessions records the requested scope synchronously at entry', async () => {
    const release = deferredGet();
    const p = useNoteChatStore.getState().loadSessions('p1', 'd1');
    expect(useNoteChatStore.getState().sessionsScope).toBe('p1:d1:note');
    release([]);
    await p;
  });

  it('scope mismatch: synchronously drops the previous doc\'s rows BEFORE the fetch resolves (no cache)', async () => {
    useNoteChatStore.setState({ sessions: [noteSession('nA')], sessionsScope: 'p1:dA:note' });
    const release = deferredGet();
    const p = useNoteChatStore.getState().loadSessions('p1', 'dB');
    const s = useNoteChatStore.getState();
    expect(s.sessions).toEqual([]);
    expect(s.sessionsScope).toBe('p1:dB:note');
    expect(s.sessionsLoading).toBe(true);
    release([]);
    await p;
  });

  it('scope mismatch: paints this scope\'s cached rows synchronously (SWR pre-paint)', async () => {
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce([noteSession('nB1')]);
    await useNoteChatStore.getState().loadSessions('p1', 'dB'); // seeds the SWR cache
    useNoteChatStore.setState({ sessions: [noteSession('nA')], sessionsScope: 'p1:dA:note' });
    const release = deferredGet();
    const p = useNoteChatStore.getState().loadSessions('p1', 'dB');
    expect(ids(useNoteChatStore.getState())).toEqual(['nB1']);
    release([noteSession('nB2')]);
    await p;
    expect(ids(useNoteChatStore.getState())).toEqual(['nB2']);
  });

  it('hydrateSessions records the scope together with the sessions', () => {
    useNoteChatStore.getState().hydrateSessions('p1', 'dB', [noteSession('nB')]);
    const s = useNoteChatStore.getState();
    expect(s.sessionsScope).toBe('p1:dB:note');
    expect(ids(s)).toEqual(['nB']);
    expect(s.sessionsLoading).toBe(false);
  });

  it('a bundle hydrate supersedes an in-flight loadSessions (no stale repaint over hydrated rows)', async () => {
    const release = deferredGet();
    const loadA = useNoteChatStore.getState().loadSessions('p1', 'dA');
    await new Promise(r => setTimeout(r, 0));
    useNoteChatStore.getState().hydrateSessions('p1', 'dB', [noteSession('nB')]);
    release([noteSession('nA')]);
    await loadA;
    const s = useNoteChatStore.getState();
    expect(ids(s)).toEqual(['nB']);
    expect(s.sessionsScope).toBe('p1:dB:note');
  });

  describe('ensureSessions', () => {
    it('resolves WITHOUT a GET when the scope already matches (zero-fetch remount)', async () => {
      useNoteChatStore.getState().hydrateSessions('p1', 'd1', [noteSession('n1')]);
      await useNoteChatStore.getState().ensureSessions('p1', 'd1');
      expect(apiClient.get).not.toHaveBeenCalled();
      expect(ids(useNoteChatStore.getState())).toEqual(['n1']);
    });

    it('fetches when the scope is null (fresh store)', async () => {
      (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      await useNoteChatStore.getState().ensureSessions('p1', 'd1');
      expect(apiClient.get).toHaveBeenCalledTimes(1);
      expect((apiClient.get as ReturnType<typeof vi.fn>).mock.calls[0][0]).toBe(
        '/chat/sessions?project_id=p1&document_id=d1&is_note=true',
      );
    });

    it('fetches on scope mismatch', async () => {
      useNoteChatStore.setState({ sessionsScope: 'p1:dOTHER:note' });
      (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([]);
      await useNoteChatStore.getState().ensureSessions('p1', 'd1');
      expect(apiClient.get).toHaveBeenCalledTimes(1);
    });
  });

  it('out-of-order: slow A resolving after fast B must NOT repaint A\'s rows (latest requested wins)', async () => {
    let releaseA!: (v: unknown) => void;
    const aPending = new Promise<unknown>(r => { releaseA = r; });
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('document_id=dA')) return aPending;
      return Promise.resolve([noteSession('nB')]);
    });

    // [A] start: deferred fetch holds the response.
    const loadA = useNoteChatStore.getState().loadSessions('p1', 'dA');
    await new Promise(r => setTimeout(r, 0));
    expect(useNoteChatStore.getState().sessionsScope).toBe('p1:dA:note');

    // [B] start + resolve: B's rows are applied under B's scope.
    const loadB = useNoteChatStore.getState().loadSessions('p1', 'dB');
    await loadB;
    expect(ids(useNoteChatStore.getState())).toEqual(['nB']);
    expect(useNoteChatStore.getState().sessionsScope).toBe('p1:dB:note');

    // [A] resolves late — must NOT clobber doc B's rows.
    releaseA([noteSession('nA')]);
    await loadA;

    const s = useNoteChatStore.getState();
    expect(ids(s)).toEqual(['nB']);
    expect(s.sessionsScope).toBe('p1:dB:note');
  });

  it('a superseded load still writes its SWR cache entry (correctly keyed)', async () => {
    let releaseA!: (v: unknown) => void;
    const aPending = new Promise<unknown>(r => { releaseA = r; });
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('document_id=dA')) return aPending;
      return Promise.resolve([noteSession('nB')]);
    });
    const loadA = useNoteChatStore.getState().loadSessions('p1', 'dA');
    const loadB = useNoteChatStore.getState().loadSessions('p1', 'dB');
    await loadB;
    releaseA([noteSession('nA')]);
    await loadA;

    // Re-open A: the entry must pre-paint from cache synchronously even though
    // the store set above was dropped by the latest-requested guard.
    const release2 = deferredGet();
    const p2 = useNoteChatStore.getState().loadSessions('p1', 'dA');
    expect(ids(useNoteChatStore.getState())).toEqual(['nA']);
    release2([noteSession('nA')]);
    await p2;
  });

  it('no-cache fetch failure leaves an honest empty list for the target scope + toast', async () => {
    useNoteChatStore.setState({ sessions: [noteSession('nA')], sessionsScope: 'p1:dA:note' });
    const showToast = vi.spyOn(useAppStore.getState(), 'showToast').mockImplementation(() => {});
    (apiClient.get as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('boom'));
    await useNoteChatStore.getState().loadSessions('p1', 'dB');
    const s = useNoteChatStore.getState();
    expect(s.sessions).toEqual([]);
    expect(s.sessionsScope).toBe('p1:dB:note');
    expect(s.sessionsLoading).toBe(false);
    expect(showToast).toHaveBeenCalledWith('Failed to load notes', 'error');
    showToast.mockRestore();
  });

  it('reset() clears the scope fields', () => {
    useNoteChatStore.setState({
      sessions: [noteSession('nA')],
      sessionsScope: 'p1:dA:note',
      lastRequestedSessionsScope: 'p1:dA:note',
    });
    useNoteChatStore.getState().reset();
    const s = useNoteChatStore.getState();
    expect(s.sessionsScope).toBeNull();
    expect(s.sessions).toEqual([]);
  });
});
