/**
 * Unit tests for NotesPanel — the defect-heavy contracts called out by the
 * WHY/INVARIANT markers in NotesPanel.tsx: link-driven anchored coloring
 * (anchorless == no [text](note:ID) link in the OWNING material, regardless of
 * reference attachment), per-reference grouping with title fallback, the
 * doc-note vs ref-note click fork, role gates (readonly sees no create/delete
 * affordances), and the no-silent-degradation create-failure toast.
 *
 * Harness: manual createRoot + act per the repo's component-test pattern
 * (ChatHeader.test.tsx). useNoteCrud is stubbed (it has its own test file) so
 * the panel's own logic is what these tests bind.
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import styles from './ui/ListPill.module.css';

// ---- store / hook mocks (mutable per test) ----
const appState: Record<string, unknown> = {
  accessLevel: 'full',
  currentProject: { project_id: 'p1' },
  currentDocument: { document_id: 'doc-1', content: '' },
  currentReference: null,
  references: [] as Array<{ reference_id: string; title: string }>,
  showToast: vi.fn(),
};
vi.mock('../store/app-store', () => ({
  useAppStore: Object.assign((sel: (s: unknown) => unknown) => sel(appState), { getState: () => appState }),
}));

const noteStoreState: Record<string, unknown> = {
  activeNoteThreadId: null,
  setActiveNoteThreadId: vi.fn(),
  setPendingNoteNavigation: vi.fn(),
  setConnectedNoteId: vi.fn(),
};
vi.mock('../store/note-store', () => ({
  useNoteStore: Object.assign((sel: (s: unknown) => unknown) => sel(noteStoreState), { getState: () => noteStoreState }),
}));

const noteChatStoreState: Record<string, unknown> = {
  sessions: [],
  sessionsLoading: false,
  sessionsScope: 'p1:doc-1:note',
  activeSessionId: null,
  addPendingImage: vi.fn(),
  pendingImages: [],
  setActiveSession: vi.fn(),
  setPendingInputFocus: vi.fn(),
  createNoteSession: vi.fn(),
};
vi.mock('../store/note-chat-store', () => ({
  useNoteChatStore: Object.assign((sel: (s: unknown) => unknown) => sel(noteChatStoreState), { getState: () => noteChatStoreState }),
}));

// Real inbox-store against a mocked API client: the read POST and the local
// summary decrement stay observable (mocking the store module would hide them).
const { apiPost } = vi.hoisted(() => ({ apiPost: vi.fn() }));
vi.mock('../api/client', () => ({
  apiClient: {
    post: apiPost,
    get: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
  },
}));

const crudState: Record<string, unknown> = {
  isRefMode: false,
  isSplitMode: false,
  noteItems: [],
  sessionsLoading: false,
  activeNoteSessionId: null,
  handleDelete: vi.fn(),
  exitThread: vi.fn(),
};
vi.mock('../hooks/useNoteCrud', () => ({ useNoteCrud: () => crudState }));

const emitted: Array<[string, unknown]> = [];
vi.mock('../events', () => ({
  emit: (...a: [string, unknown]) => { emitted.push([a[0], a[1]]); },
  // inbox-store imports on/off for useInboxSummary (not exercised here).
  on: () => {},
  off: () => {},
}));

vi.mock('../editor/active-editor', () => ({ getRoleView: () => null }));
vi.mock('../hooks/useArmedAction', () => ({
  useArmedAction: () => ({ armed: false, handleClick: (fn: () => void) => fn(), disarm: () => {} }),
}));
vi.mock('../hooks/useImageDropHandlers', () => ({
  useImageDropHandlers: () => ({ isDragging: false, dragHandlers: {} }),
}));
vi.mock('../hooks/useRightPanelHoverPreview', () => ({
  useRightPanelHoverPreview: () => ({ visible: false, handleHover: vi.fn(), handleHoverLeave: vi.fn() }),
}));
vi.mock('../hooks/useReferencePreview', () => ({
  useReferencePreview: () => ({ preview: null, error: false, loading: false }),
}));
vi.mock('./NoteChatView', () => ({ NoteChatView: () => createElement('div', { 'data-testid': 'note-thread' }) }));
vi.mock('./HoverPreviewPopup', () => ({
  HoverPreviewPopup: ({ visible }: { visible: boolean }) =>
    createElement('div', { 'data-testid': 'hover-preview', 'data-visible': String(visible) }),
}));
vi.mock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

import { NotesPanel } from './NotesPanel';
import { useInboxStore } from '../store/inbox-store';

let root: Root | null = null;
function mountPanel() {
  const host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(createElement(NotesPanel)));
  return host;
}
function unmount() {
  // Capture into a local: `root` is a mutable module-level binding, so TS drops
  // the null-narrowing inside the act() closure.
  const r = root;
  if (r) act(() => r.unmount());
  root = null;
}

/** A note session with sensible defaults; override per test. */
function note(overrides: Record<string, unknown> & { session_id: string }) {
  return {
    reference_id: null,
    first_message_preview: 'preview ' + overrides.session_id,
    message_count: 1,
    created_at: '2026-01-01T00:00:00Z',
    anchor_rel_start: null,
    anchor_offset_start: null,
    ...overrides,
  };
}

function pillVariant(sessionId: string): string | null {
  const el = document.getElementById(`note-${sessionId}`);
  if (!el) return null;
  const map = styles as Record<string, string>;
  for (const v of ['anchored', 'anchorless', 'highlight', 'plain', 'backlink']) {
    // styles[v] is the same class token the component composed — binds the
    // variant decision to the rendered class, not to a test-local literal.
    if (el.classList.contains(map[v])) return v;
  }
  return null;
}

beforeEach(() => {
  vi.clearAllMocks();
  emitted.length = 0;
  appState.accessLevel = 'full';
  appState.currentProject = { project_id: 'p1' };
  appState.currentDocument = { document_id: 'doc-1', content: '' };
  appState.currentReference = null;
  appState.references = [];
  noteStoreState.activeNoteThreadId = null;
  noteChatStoreState.createNoteSession = vi.fn();
  noteChatStoreState.sessionsScope = 'p1:doc-1:note';
  apiPost.mockReset().mockResolvedValue(undefined);
  useInboxStore.setState({ summary: {}, toggles: {} });
  Object.assign(crudState, {
    isRefMode: false,
    isSplitMode: false,
    noteItems: [],
    sessionsLoading: false,
    activeNoteSessionId: null,
    handleDelete: vi.fn(),
    exitThread: vi.fn(),
  });
});

describe('NotesPanel', () => {
  it('groups reference notes under their reference title, doc notes stay ungrouped; unknown ref falls back to the raw id', () => {
    appState.references = [
      { reference_id: 'ref-1', title: 'Ref One' },
    ];
    crudState.noteItems = [
      note({ session_id: 'n-doc' }),
      note({ session_id: 'n-ref1', reference_id: 'ref-1' }),
      note({ session_id: 'n-refx', reference_id: 'ref-unknown' }),
    ];
    const host = mountPanel();
    // Group headers: known title, and the raw-id fallback for an unknown ref.
    expect(host.textContent).toContain('Ref One');
    expect(host.textContent).toContain('ref-unknown');
    // Doc notes have no group header — the doc group title is ''. Only ref
    // group headers carry a clickable truncate span as a DIRECT child.
    const headers = Array.from(host.querySelectorAll('div.cursor-pointer > span.truncate'));
    expect(headers.map((h) => h.textContent)).toEqual(['Ref One', 'ref-unknown']);
    unmount();
  });

  it('pins the anchored-color INVARIANT — driven ONLY by a body note: link; a reference-attached note with no link stays anchorless', () => {
    appState.currentDocument = { document_id: 'doc-1', content: 'see [frag](note:n-linked)' };
    crudState.noteItems = [
      note({ session_id: 'n-linked' }),
      note({ session_id: 'n-ref-attached', reference_id: 'ref-1' }),
    ];
    mountPanel();
    expect(pillVariant('n-linked')).toBe('anchored');
    // Attached to a reference but the OWNING material has no link → grey.
    expect(pillVariant('n-ref-attached')).toBe('anchorless');
    unmount();
  });

  it('anchor_offset hint colors a freshly-created (link-not-yet-propagated) note anchored', () => {
    appState.currentDocument = { document_id: 'doc-1', content: 'no links at all' };
    crudState.noteItems = [note({ session_id: 'n-fresh', anchor_offset_start: 4 })];
    mountPanel();
    expect(pillVariant('n-fresh')).toBe('anchored');
    unmount();
  });

  it('doc-note click opens the thread and scrolls the editor; anchor hint connects the pin', () => {
    appState.currentDocument = { document_id: 'doc-1', content: '' };
    crudState.noteItems = [note({ session_id: 'n-open', anchor_rel_start: { kind: 'relative' } })];
    const host = mountPanel();
    act(() => document.getElementById('note-n-open')!.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(noteStoreState.setActiveNoteThreadId).toHaveBeenCalledWith('n-open', 'doc-1');
    expect(noteChatStoreState.setActiveSession).toHaveBeenCalledWith('n-open');
    expect(noteChatStoreState.setPendingInputFocus).toHaveBeenCalledWith(true);
    expect(emitted.map((e) => e[0])).toContain('scroll-to-note-in-editor');
    expect(noteStoreState.setConnectedNoteId).toHaveBeenCalledWith('n-open');
    unmount();
  });

  it('ref-note click from doc mode navigates to the reference and does NOT open the thread here', () => {
    crudState.noteItems = [note({ session_id: 'n-refnav', reference_id: 'ref-9' })];
    const host = mountPanel();
    act(() => document.getElementById('note-n-refnav')!.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(noteStoreState.setPendingNoteNavigation).toHaveBeenCalledWith({ noteId: 'n-refnav' });
    expect(emitted).toContainEqual(['navigate-to-reference', { referenceId: 'ref-9' }]);
    expect(noteStoreState.setActiveNoteThreadId).not.toHaveBeenCalled();
    unmount();
  });

  it('readonly: no add-note header, no delete buttons, plain empty state — not the authoring hint', () => {
    appState.accessLevel = 'readonly';
    crudState.noteItems = [note({ session_id: 'n-ro' })];
    const host = mountPanel();
    expect(host.textContent).not.toContain('addNote');
    expect(host.querySelector('[title="deleteNote"]')).toBeNull();
    unmount();

    crudState.noteItems = [];
    const host2 = mountPanel();
    expect(host2.textContent).toContain('noNotes');
    expect(host2.textContent).not.toContain('noNotesYetHint');
    unmount();
  });

  it('full access sees the add-note affordance and per-card delete', () => {
    crudState.noteItems = [note({ session_id: 'n-full' })];
    const host = mountPanel();
    expect(host.textContent).toContain('addNote');
    expect(host.querySelector('[title="deleteNote"]')).not.toBeNull();
    unmount();
  });

  it('create failure shows the error toast (no silent degradation)', async () => {
    noteChatStoreState.createNoteSession = vi.fn().mockRejectedValue(new Error('boom'));
    const host = mountPanel();
    const addBtn = Array.from(host.querySelectorAll('button')).find((b) => b.textContent?.includes('addNote'));
    expect(addBtn).toBeTruthy();
    await act(async () => {
      addBtn!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(appState.showToast).toHaveBeenCalledWith('failedToCreateNote', 'error');
    unmount();
  });

  it('thread view replaces the list: back header + NoteChatView', () => {
    noteStoreState.activeNoteThreadId = 'n-t1';
    crudState.activeNoteSessionId = 'n-t1';
    crudState.noteItems = [note({ session_id: 'n-t1' })];
    const host = mountPanel();
    expect(host.textContent).toContain('backToAllNotes');
    expect(host.querySelector('[data-testid="note-thread"]')).not.toBeNull();
    expect(document.getElementById('note-n-t1')).toBeNull();
    unmount();
  });

  // ── SYSTEM: inbox — unread-flagged external notes ─────────────────────────
  // Fresh doc ids per test: the auto-open once-per-doc guard is a module-level
  // Set (it must survive remounts — see the panel source), so re-using a doc id
  // across tests would silently skip its auto-open.
  it('auto-opens the EARLIEST unread note on mount (created_at ASC): thread opens, read POST fires, summary decrements, pill painted', async () => {
    appState.currentDocument = { document_id: 'doc-ia', content: '' };
    noteChatStoreState.sessionsScope = 'p1:doc-ia:note';
    crudState.noteItems = [
      note({ session_id: 'n-later', unread: true, created_at: '2026-03-02T00:00:00Z' }),
      note({ session_id: 'n-early', unread: true, created_at: '2026-03-01T00:00:00Z' }),
    ];
    useInboxStore.setState({ summary: { 'doc-ia': { notes: 2, refs: 0 } } });
    mountPanel();
    // Flush the openInboxObject microtask chain (POST mock → applyRead).
    await act(async () => { await Promise.resolve(); });
    expect(noteStoreState.setActiveNoteThreadId).toHaveBeenCalledWith('n-early', 'doc-ia');
    expect(apiPost).toHaveBeenCalledWith('/inbox/read', { kind: 'note', id: 'n-early' });
    expect(useInboxStore.getState().summary['doc-ia'].notes).toBe(1);
    // Both flagged pills carry the unread mark class (composed by ListPill).
    const map = styles as Record<string, string>;
    expect(document.getElementById('note-n-early')!.classList.contains(map.unread)).toBe(true);
    expect(document.getElementById('note-n-later')!.classList.contains(map.unread)).toBe(true);
    unmount();
  });

  it('auto-open fires once per document: a remount does not drag the user back in', async () => {
    appState.currentDocument = { document_id: 'doc-ib', content: '' };
    noteChatStoreState.sessionsScope = 'p1:doc-ib:note';
    crudState.noteItems = [note({ session_id: 'n-once', unread: true })];
    useInboxStore.setState({ summary: { 'doc-ib': { notes: 1, refs: 0 } } });
    mountPanel();
    await act(async () => { await Promise.resolve(); });
    expect(noteStoreState.setActiveNoteThreadId).toHaveBeenCalledTimes(1);
    unmount();
    // Simulate the user leaving the thread, then reopening the notes tab.
    noteStoreState.activeNoteThreadId = null;
    (noteStoreState.setActiveNoteThreadId as ReturnType<typeof vi.fn>).mockClear();
    mountPanel();
    await act(async () => { await Promise.resolve(); });
    expect(noteStoreState.setActiveNoteThreadId).not.toHaveBeenCalled();
    unmount();
  });

  it('no auto-open when a thread is already open (no intrusion into an open read)', () => {
    appState.currentDocument = { document_id: 'doc-ic', content: '' };
    noteChatStoreState.sessionsScope = 'p1:doc-ic:note';
    noteStoreState.activeNoteThreadId = 'n-open';
    crudState.activeNoteSessionId = 'n-open';
    crudState.noteItems = [note({ session_id: 'n-open' }), note({ session_id: 'n-flag', unread: true })];
    mountPanel();
    expect(apiPost).not.toHaveBeenCalled();
    unmount();
  });

  it('no auto-open while the loaded list belongs to another document (sessionsScope mismatch)', () => {
    appState.currentDocument = { document_id: 'doc-id', content: '' };
    // Scope left at doc-1 — the stale doc-switch render window.
    crudState.noteItems = [note({ session_id: 'n-stale', unread: true })];
    mountPanel();
    expect(apiPost).not.toHaveBeenCalled();
    expect(noteStoreState.setActiveNoteThreadId).not.toHaveBeenCalled();
    unmount();
  });

  it('clicking an unread note fires the read POST and decrements the summary', async () => {
    appState.currentDocument = { document_id: 'doc-id', content: '' };
    // Scope mismatch suppresses the auto-open so the CLICK path is what fires.
    crudState.noteItems = [note({ session_id: 'n-click', unread: true })];
    useInboxStore.setState({ summary: { 'doc-id': { notes: 1, refs: 0 } } });
    const host = mountPanel();
    act(() => document.getElementById('note-n-click')!.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    await act(async () => { await Promise.resolve(); });
    expect(apiPost).toHaveBeenCalledWith('/inbox/read', { kind: 'note', id: 'n-click' });
    expect(useInboxStore.getState().summary['doc-id'].notes).toBe(0);
    expect(noteStoreState.setActiveNoteThreadId).toHaveBeenCalledWith('n-click', 'doc-id');
    unmount();
  });
});
