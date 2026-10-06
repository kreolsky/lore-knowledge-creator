/**
 * MessageList — scroll ownership during a streaming turn, and the assembled
 * turn's per-row node slice (SYSTEM: dsh-conversation).
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Streamable test state; each test mutates `streaming`/`activePath` before
// mount so the component reads a realistic snapshot through the selector.
const chatState: Record<string, unknown> = {
  activePath: [],
  streaming: null,
  // The assembler's published timeline (SYSTEM: dsh-conversation) — empty in
  // these tests, so the bubbles render through the legacy segment branches.
  conversation: [],
  turnRanges: {},
  turnStartSeq: null,
  messagesLoading: false,
  messagesError: null,
  chatScopeLoading: false,
  activeSessionId: 's1',
  sessions: [],
  loadMessages: vi.fn(),
  editMessage: vi.fn(),
  deleteMessage: vi.fn(),
  forkAndResend: vi.fn(),
  regenerate: vi.fn(),
  getSiblings: vi.fn(() => []),
  selectSibling: vi.fn(),
  // Rewind-to-message: the component reads the sentinel + the two actions
  // from the store.
  selectedSiblings: {},
  rewindTo: vi.fn(),
  cancelRewind: vi.fn(),
  sendMessage: vi.fn(),
  decideVerdict: vi.fn(),
};
vi.mock('../../store/chat-store', () => ({
  useChatStore: Object.assign((sel: (s: unknown) => unknown) => sel(chatState), {
    getState: () => chatState,
  }),
  selectActivePath: (s: { activePath: unknown[] }) => s.activePath,
}));
const appState: Record<string, unknown> = {
  currentProject: null,
  currentDocument: null,
  documents: [],
  showToast: vi.fn(),
  setDocuments: vi.fn(),
};
vi.mock('../../store/app-store', () => ({
  useAppStore: Object.assign((sel: (s: unknown) => unknown) => sel(appState), { getState: () => appState }),
}));
vi.mock('../../i18n', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
  t: (k: string) => k,
}));
vi.mock('../../events', () => ({ on: vi.fn(), off: vi.fn(), emit: vi.fn() }));
vi.mock('../../hooks/useArmedAction', () => ({
  useArmedAction: () => ({ armed: false, handleClick: (fn: () => void) => fn(), disarm: () => {} }),
}));
vi.mock('../../api/client', () => ({ apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn() } }));

import { MessageList } from './MessageList';
import { apiClient } from '../../api/client';

// jsdom lacks Element.scrollTo (the MessageList follow effect calls it on the
// scroll container); polyfill like the ResizeObserver polyfill in
// MessageBubble.test.tsx.
if (!Element.prototype.scrollTo) {
  (Element.prototype as unknown as { scrollTo: () => void }).scrollTo = () => {};
}

function mount() {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(createElement(MessageList)));
  return { host, root };
}

function assistantMessage() {
  return {
    message_id: 'a1',
    chat_id: 's1',
    parent_id: null,
    role: 'assistant',
    content: '',
    segments: [],
    created_at: '2026-05-20T14:59:00Z',
  };
}

function scrollContainer(host: HTMLElement): HTMLElement {
  return host.firstElementChild as HTMLElement;
}

function stubScrollMetrics(el: HTMLElement, scrollHeight: number, clientHeight: number) {
  Object.defineProperty(el, 'scrollHeight', { configurable: true, value: scrollHeight });
  Object.defineProperty(el, 'clientHeight', { configurable: true, value: clientHeight });
}

// jsdom does no layout: give the element a plain stored scrollTop (no clamping)
// so writes/reads by the component are observable.
function trackScrollTop(el: HTMLElement) {
  let value = 0;
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => value,
    set: (v: number) => { value = v; },
  });
}

// Reads the box object at CALL time, so mutating it between renders simulates
// layout growth for the NEXT pass.
function stubBox(el: HTMLElement, box: { top: number; bottom: number }) {
  vi.spyOn(el, 'getBoundingClientRect').mockImplementation(() => ({
    top: box.top,
    bottom: box.bottom,
    left: 0,
    right: 0,
    width: 0,
    height: box.bottom - box.top,
    x: 0,
    y: box.top,
    toJSON: () => ({}),
  } as DOMRect));
}

function streamingState(content: string) {
  return {
    messageId: 'a1',
    content,
    controller: null,
  };
}

describe('MessageList — scroll ownership (plan reasoning-growth-stops-shaking-tool-chips)', () => {
  it('growing streaming content while stuck writes scrollTop synchronously and never calls scrollTo', () => {
    chatState.activePath = [assistantMessage()];
    chatState.streaming = streamingState('abc');
    const { host, root } = mount();
    const el = scrollContainer(host);
    stubScrollMetrics(el, 1000, 400);
    trackScrollTop(el);
    el.scrollTop = 0; // drifted content, but the user has NOT scrolled — stick stays true
    const scrollToSpy = vi.spyOn(el, 'scrollTo');
    chatState.streaming = streamingState('abcdef');
    act(() => root.render(createElement(MessageList)));
    expect(el.scrollTop).toBe(1000);
    expect(scrollToSpy).not.toHaveBeenCalled();
    act(() => root.unmount());
    host.remove();
  });

  it('growing content while NOT stuck raises scrollTop by exactly the anchored child layout delta (no drift, no smooth scroll)', () => {
    const second = { ...assistantMessage(), message_id: 'a2' };
    chatState.activePath = [assistantMessage(), second];
    chatState.streaming = { ...streamingState('abc'), messageId: 'a2' };
    const { host, root } = mount();
    const el = scrollContainer(host);
    stubScrollMetrics(el, 3000, 400);
    trackScrollTop(el);
    const bubbles = Array.from(el.children) as HTMLElement[];
    // b0 fully above the viewport top edge; b1 (the streaming bubble) straddles it.
    const box0 = { top: -600, bottom: -100 };
    const box1 = { top: 150, bottom: 2000 };
    stubBox(bubbles[0], box0);
    stubBox(bubbles[1], box1);
    stubBox(el, { top: 0, bottom: 400 });
    // User scrolled up mid-stream: the scroll event flips stickToBottomRef off.
    el.scrollTop = 500;
    el.dispatchEvent(new Event('scroll'));
    const scrollToSpy = vi.spyOn(el, 'scrollTo');
    // Pass 1 (not stuck): picks the topmost visible element as anchor, no correction yet.
    chatState.streaming = { ...streamingState('abcdef'), messageId: 'a2' };
    act(() => root.render(createElement(MessageList)));
    expect(el.scrollTop).toBe(500);
    // Layout above the anchor grew by 60 → anchor moved down 60.
    box1.top = 210;
    chatState.streaming = { ...streamingState('abcdefabcdef'), messageId: 'a2' };
    act(() => root.render(createElement(MessageList)));
    // scrollTop compensated +60 → the anchor's viewport-relative position is unchanged.
    expect(el.scrollTop).toBe(560);
    expect(scrollToSpy).not.toHaveBeenCalled();
    act(() => root.unmount());
    host.remove();
  });

  it('a new entry in the active path still glides: scrollTo with behavior smooth', () => {
    chatState.activePath = [assistantMessage()];
    chatState.streaming = null;
    const { host, root } = mount();
    const el = scrollContainer(host);
    stubScrollMetrics(el, 1500, 400);
    trackScrollTop(el);
    const scrollToSpy = vi.spyOn(el, 'scrollTo');
    chatState.activePath = [assistantMessage(), { ...assistantMessage(), message_id: 'a2' }];
    act(() => root.render(createElement(MessageList)));
    expect(scrollToSpy).toHaveBeenCalledTimes(1);
    expect(scrollToSpy).toHaveBeenCalledWith(expect.objectContaining({ behavior: 'smooth' }));
    act(() => root.unmount());
    host.remove();
  });
});

describe('MessageList — the assembled turn (SYSTEM: dsh-conversation)', () => {
  // The node the feed publishes for a growing assistant step: its key is
  // STABLE while its data grows, so the per-row slice reuse must compare node
  // IDENTITY. A coarser test (length + last key) reuses the stale slice and the
  // reply stops advancing mid-turn while the tokens keep arriving.
  const step = (text: string) => ({
    key: 'assistant-step:0:0',
    kind: 'assistant-step',
    anchorSeq: 10,
    data: { blocks: [{ kind: 'text', text }] },
  });

  function streamingTurn(nodes: unknown[]) {
    chatState.activePath = [assistantMessage()];
    chatState.streaming = {
      messageId: 'a1', content: '', controller: null,
    };
    chatState.conversation = nodes;
    chatState.turnRanges = {};
    chatState.turnStartSeq = 5;
  }

  it('advances the streamed text when the step node grows under a stable key', () => {
    streamingTurn([step('Hel')]);
    const { host, root } = mount();
    expect(host.textContent).toContain('Hel');

    streamingTurn([step('Hello there')]);
    act(() => root.render(createElement(MessageList)));
    expect(host.textContent).toContain('Hello there');

    act(() => root.unmount());
    host.remove();
    chatState.conversation = [];
    chatState.turnStartSeq = null;
  });

  it('renders the settled row from its turn window, not from content', () => {
    chatState.activePath = [{ ...assistantMessage(), content: 'the row copy' }];
    chatState.streaming = null;
    chatState.conversation = [step('the assembled reply')];
    chatState.turnRanges = { a1: { min: 1, max: 20 } };
    chatState.turnStartSeq = null;
    const { host, root } = mount();
    expect(host.textContent).toContain('the assembled reply');
    expect(host.textContent).not.toContain('the row copy');
    act(() => root.unmount());
    host.remove();
    chatState.conversation = [];
    chatState.turnRanges = {};
  });
});

describe('MessageList — empty-content guard (plan chat-message-content-empty-until-reload)', () => {
  // The row's `content` is what both buttons act on (MessageBubble passes
  // message.content). With no `done`-frame text it is '' on a fresh reply —
  // the buttons must warn, never write the clipboard nor POST a document.
  it('copy and create-document on an empty assistant row warn instead of acting', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    (appState.showToast as ReturnType<typeof vi.fn>).mockClear();
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear();
    // A resolvable project/document, so the guard — not a missing context —
    // is what stops the POST.
    appState.currentProject = { project_id: 'p1' };
    appState.currentDocument = { document_id: 'd1' };
    chatState.activePath = [assistantMessage()];
    chatState.streaming = null;
    const { host, root } = mount();
    const copyBtn = host.querySelector('button[title="copy"]') as HTMLButtonElement;
    const docBtn = host.querySelector('button[title="createDocumentFromChat"]') as HTMLButtonElement;
    act(() => copyBtn.click());
    expect(writeText).not.toHaveBeenCalled();
    expect(appState.showToast).toHaveBeenCalledWith('replyTextNotReady', 'warning');
    act(() => docBtn.click());
    expect(apiClient.post).not.toHaveBeenCalled();
    expect(appState.showToast).toHaveBeenCalledTimes(2);
    act(() => root.unmount());
    host.remove();
    appState.currentProject = null;
    appState.currentDocument = null;
  });
});

describe('MessageList — rewind plaque', () => {
  function userMessage(id: string) {
    return { message_id: id, chat_id: 's1', parent_id: null, role: 'user', content: id, created_at: '2026-05-20T14:59:00Z' };
  }

  function renderWith(activePath: unknown[], selectedSiblings: Record<string, string>) {
    chatState.activePath = activePath;
    chatState.selectedSiblings = selectedSiblings;
    chatState.streaming = null;
    (chatState.cancelRewind as ReturnType<typeof vi.fn>).mockClear();
    return mount();
  }

  // jsdom lacks ResizeObserver; the user-bubble width-measurement effect uses it.
  let prevRO: unknown;
  beforeEach(() => {
    prevRO = (globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver;
    (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  });
  afterEach(() => {
    (globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver = prevRO;
  });

  function cleanup({ host, root }: { host: HTMLElement; root: Root }) {
    act(() => root.unmount());
    host.remove();
    chatState.activePath = [];
    chatState.selectedSiblings = {};
  }

  it('a path ending at the cut shows the plaque; its cancel calls cancelRewind', () => {
    const mounted = renderWith([userMessage('u1')], { u1: '__rewind__' });
    expect(mounted.host.textContent).toContain('rewindActive');
    const cancel = Array.from(mounted.host.querySelectorAll('button')).find(b => b.textContent === 'cancel')!;
    act(() => cancel.click());
    expect(chatState.cancelRewind).toHaveBeenCalledTimes(1);
    cleanup(mounted);
  });

  it('a sentinel off the rendered path shows no plaque', () => {
    const mounted = renderWith([userMessage('u1')], { elsewhere: '__rewind__' });
    expect(mounted.host.textContent).not.toContain('rewindActive');
    cleanup(mounted);
  });

  it('a rewound first message renders an empty conversation with the plaque, not the chat picker', () => {
    const mounted = renderWith([], { __root__: '__rewind__' });
    expect(mounted.host.textContent).toContain('rewindActive');
    // The conversation scroll container, holding only the plaque — no message rows.
    const list = scrollContainer(mounted.host);
    expect(list.className).toContain('overflow-y-auto');
    expect(list.childElementCount).toBe(1);
    cleanup(mounted);
  });
});
