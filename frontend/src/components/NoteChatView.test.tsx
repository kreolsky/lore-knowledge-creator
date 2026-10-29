/** Plan chat-draft-persistence (component round-trip): the note composer text must
 * survive NoteChatView unmount (back-to-list / tab switch / Esc) because it lives in
 * note-chat-store's PER-SESSION drafts, not local useState. Mount → type → unmount →
 * remount restores the open thread's draft. */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// jsdom does not implement Element.scrollTo (the view's stick-to-bottom effect
// calls it with options) — orthogonal to the draft behavior under test.
Element.prototype.scrollTo = () => {};

const appStoreState: Record<string, unknown> = {
  currentUser: null,
  currentProject: null,
  showToast: vi.fn(),
  maxAttachmentMb: 5,
  currentDocument: null,
};
vi.mock('../store/app-store', () => ({
  useAppStore: Object.assign(
    (sel: (s: unknown) => unknown) => sel(appStoreState),
    { getState: () => appStoreState },
  ),
}));
// The shared exit path runs against the REAL note-store + note-chat-store;
// only ui-store stays mocked, with a hoisted spy for the restore call.
const { setRightPanelTabMock } = vi.hoisted(() => ({ setRightPanelTabMock: vi.fn() }));
vi.mock('../store/ui-store', () => ({
  useUIStore: {
    getState: () => ({ setRightPanelTab: setRightPanelTabMock, getRefOpenMode: () => 'center' }),
  },
}));
vi.mock('../api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
vi.mock('../i18n', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
  t: (k: string) => k,
}));
vi.mock('../hooks/useSimpleVoiceRecording', () => ({
  useSimpleVoiceRecording: () => ({ recording: voice.recording, transcribing: false, toggleRecording: vi.fn(), cancelRecording: vi.fn() }),
}));
vi.mock('./chat/MessageBubble', () => ({ MessageBubble: () => null }));
vi.mock('./chat/shared/copy', () => ({ copyWithToast: vi.fn() }));
// Reduced to the contract under test (see ChatInput.test.tsx for the rationale).
vi.mock('./chat/ChatComposer', () => ({
  ChatComposer: (props: {
    value: string;
    onChange: (e: { target: { value: string } }) => void;
    textareaRef: { current: HTMLTextAreaElement | null };
    onKeyDown?: unknown;
  }) =>
    createElement('textarea', {
      value: props.value,
      onChange: props.onChange as never,
      onKeyDown: props.onKeyDown as never,
      ref: props.textareaRef as never,
    }),
}));

import { NoteChatView } from './NoteChatView';
import { useNoteChatStore } from '../store/note-chat-store';
import { useNoteStore } from '../store/note-store';

function render(node: ReturnType<typeof createElement>): { container: HTMLElement; root: Root } {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => { root.render(node); });
  return { container, root };
}

function unmount(rig: { container: HTMLElement; root: Root }) {
  act(() => { rig.root.unmount(); });
  rig.container.remove();
}

function textareaOf(container: HTMLElement): HTMLTextAreaElement {
  const ta = container.querySelector('textarea');
  if (!ta) throw new Error('no textarea rendered');
  return ta;
}

function typeInto(ta: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!;
  act(() => {
    setter.call(ta, value);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function seedOpenThread() {
  useNoteChatStore.setState({
    sessions: [{
      session_id: 'ns1', project_id: 'p1', document_id: 'd1', reference_id: null,
      is_note: true, title: '', mode: 'chat', model: '',
      updated_at: '2026-08-17T10:00:00Z', created_at: '2026-08-17T10:00:00Z',
    } as never],
    activeSessionId: 'ns1',
    messages: [],
    messagesLoading: false,
    drafts: {},
  });
}

beforeEach(() => {
  seedOpenThread();
  setRightPanelTabMock.mockClear();
  useNoteStore.setState({
    activeNoteThreadId: null,
    documentNoteThreads: {},
    pendingNoteNavigation: null,
    connectedNoteId: null,
    previousRightTab: null,
  });
});

const voice = { recording: false };

describe('NoteChatView draft round-trip (unmount on back/Esc/tab switch)', () => {
  it('text typed before unmount is restored after remount', () => {
    const first = render(createElement(NoteChatView));
    typeInto(textareaOf(first.container), 'note draft');

    // Back to the list: the view unmounts — the thread draft must survive.
    unmount(first);
    expect(useNoteChatStore.getState().drafts.ns1).toBe('note draft');

    // Reopen the thread: a fresh mount initializes from the per-session draft.
    const second = render(createElement(NoteChatView));
    expect(textareaOf(second.container).value).toBe('note draft');
    unmount(second);
  });
});

describe('NoteChatView Escape never deletes a note on an unproven empty', () => {
  const pressEscape = (ta: HTMLTextAreaElement) => {
    act(() => { ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })); });
  };

  it('Escape after an existing note failed to load its messages leaves it in place', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.delete as ReturnType<typeof vi.fn>).mockClear();
    // Failed load: messages stay [], loading cleared — the server row still says 4.
    useNoteChatStore.setState(s => ({ sessions: s.sessions.map(r => ({ ...r, message_count: 4 })) }));
    const rig = render(createElement(NoteChatView));
    pressEscape(textareaOf(rig.container));
    expect(apiClient.delete).not.toHaveBeenCalled();
    expect(useNoteChatStore.getState().sessions.map(r => r.session_id)).toEqual(['ns1']);
    unmount(rig);
  });
});

// ── shared exit path ──────────────────────────────────────────────────────────
// Escape must route through the ONE exitNoteThread: discard an empty thread,
// clear the thread state, restore previousRightTab when it was set.
describe('NoteChatView Escape leaves via the shared exit path', () => {
  const pressEscape = (ta: HTMLTextAreaElement) => {
    act(() => { ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })); });
  };

  it('deletes an empty thread and restores previousRightTab', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.delete as ReturnType<typeof vi.fn>).mockClear();
    // Provably empty: the server count is 0 and the load finished.
    useNoteChatStore.setState(s => ({ sessions: s.sessions.map(r => ({ ...r, message_count: 0 })) }));
    useNoteStore.setState({ previousRightTab: 'chat' });

    const rig = render(createElement(NoteChatView));
    pressEscape(textareaOf(rig.container));

    expect(apiClient.delete).toHaveBeenCalledWith('/chat/sessions/ns1');
    expect(useNoteChatStore.getState().activeSessionId).toBeNull();
    expect(useNoteStore.getState().activeNoteThreadId).toBeNull();
    expect(useNoteStore.getState().previousRightTab).toBeNull();
    expect(setRightPanelTabMock).toHaveBeenCalledWith(null, 'chat');
    unmount(rig);
  });

  it('stays on the notes tab when previousRightTab was never set (anchorless note)', () => {
    useNoteChatStore.setState(s => ({ sessions: s.sessions.map(r => ({ ...r, message_count: 0 })) }));

    const rig = render(createElement(NoteChatView));
    pressEscape(textareaOf(rig.container));

    expect(setRightPanelTabMock).not.toHaveBeenCalled();
    expect(useNoteChatStore.getState().activeSessionId).toBeNull();
    unmount(rig);
  });
});
