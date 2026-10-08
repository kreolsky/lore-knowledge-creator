/** The chat message queue (store layer): the queue lives in chat-store, keyed by
 * session; sendMessage routes to it while streaming; a clean turn-end auto-flushes ONE
 * coalesced message; abort/error/halted/lost restore the text to the composer instead.
 *
 * Driven over the REAL transport: POST /completions is the mocked apiClient, and the
 * turn's frames (and its end) arrive through dispatchChatFrame — the project-WS sink.
 *
 * The flush is session-owned: queue for session A fires when A's turn ends even while
 * the user is looking at B (the single-session streaming model requires A to be active
 * for its follow-up turn to render, so the flush brings the user to A). */
// @vitest-environment jsdom

import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('../../api/client', () => {
  class HttpError extends Error {
    status: number;
    constructor(s: number) { super(`HTTP ${s}`); this.name = 'HttpError'; this.status = s; }
  }
  class RequestTooLargeError extends Error {}
  return {
    apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
    HttpError,
    RequestTooLargeError,
  };
});

vi.mock('../app-store', () => {
  const state = {
    currentDocument: null,
    currentReference: null,
    currentProject: null,
    currentUser: { user_id: 'u1', name: 'U' },
    maxAttachmentMb: 5,
    setMaxAttachmentMb: () => {},
    accessLevel: 'full',
    showToast: vi.fn(),
  };
  return { useAppStore: { getState: vi.fn(() => state), subscribe: () => () => {} } };
});

vi.mock('../ui-store', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../ui-store')>();
  return {
    ...actual,
    useUIStore: {
      getState: vi.fn(() => ({
        documents: {},
        setLastActiveChatSession: vi.fn(),
        getLastActiveChatSession: vi.fn(() => null),
        getRefOpenMode: vi.fn(() => 'center'),
      })),
    },
  };
});

vi.mock('../../chat/context', () => ({
  setupChatContextBridge: vi.fn(),
  GHOST_SESSION_ID: '__ghost__',
  resolveCompletionContext: vi.fn(() => ({ document_ids: [], reference_ids: [] })),
  getContextForSession: vi.fn(() => ({ documentIds: [], referenceIds: [] })),
  setContextForSession: vi.fn(),
  clearContextForSession: vi.fn(),
  getDerivedGhostContext: vi.fn(() => ({ docIds: [], refIds: [] })),
  ghostBaseTargets: vi.fn(() => ({ docs: [], refs: [] })),
  resetGhostDeltas: vi.fn(),
  hydrateFromSessions: vi.fn(),
  pruneContext: vi.fn(),
  clearPendingContextPatches: vi.fn(),
  useChatContext: vi.fn(() => ({ documentIds: [], referenceIds: [] })),
  addItemToContext: vi.fn(() => Promise.resolve()),
  removeItemFromContext: vi.fn(() => Promise.resolve()),
}));

vi.mock('../../i18n', () => ({ t: (k: string) => k }));

import { useChatStore } from '../chat-store';
import { dispatchChatFrame, adoptOpenTurn } from './streaming';
import { clearChatCaches } from './reset-registry';
import { scheduleTurnEndFlush, drainQueuedOnLoad } from './queue-slice';
import type { ChatState } from './types';
import { apiClient, HttpError } from '../../api/client';
import { useAppStore } from '../app-store';
import type { ChatMessage, ChatSession } from '../../types';

const post = apiClient.post as ReturnType<typeof vi.fn>;
const showToast = () => (useAppStore.getState() as unknown as { showToast: ReturnType<typeof vi.fn> }).showToast;

function frame(f: Record<string, unknown>, sessionId = 'A') {
  dispatchChatFrame(useChatStore.getState, useChatStore.setState, sessionId, f);
}
const IDS = { type: 'ids', user_message_id: 'um', assistant_message_id: 'am' };
const TURN_CLOSED = { type: 'turn_closed' };

function sess(id: string): ChatSession {
  return {
    session_id: id, project_id: 'p1', document_id: 'd1', reference_id: null, user_id: 'u1',
    title: `t-${id}`, model: 'm', system_prompt_id: null, context_ids: [],
    created_at: '', updated_at: '',
  } as unknown as ChatSession;
}

/** POST /completions calls only (Stop also POSTs a cancel). */
function completionPosts(): Array<{ url: string; body: { messages: Array<{ content: string }> } }> {
  return post.mock.calls
    .filter(c => String(c[0]).endsWith('/completions'))
    .map(c => ({ url: String(c[0]), body: c[1] as { messages: Array<{ content: string }> } }));
}
function lastUserContent(): string {
  const calls = completionPosts();
  const msgs = calls[calls.length - 1].body.messages;
  return msgs[msgs.length - 1].content;
}
const tick = () => new Promise(r => setTimeout(r, 0));

/** Open a turn in session `sid` the way the composer does: sendMessage on an idle
 * slot → POST → ids over the WS. Returns the pending send. */
function openTurn(sid = 'A'): Promise<void> {
  const p = useChatStore.getState().sendMessage('kickoff');
  frame(IDS, sid);
  return p;
}

// Captured before any test stubs it: 'flush is session-owned' replaces
// loadMessages with a vi.fn on the module-level store — without this restore
// the stub leaks into every later test (setActiveSession then no-ops its load).
const realLoadMessages = useChatStore.getState().loadMessages;

beforeEach(() => {
  clearChatCaches();
  post.mockReset();
  post.mockResolvedValue({ accepted: true });
  showToast().mockClear();
  useChatStore.setState({
    sessions: [sess('A')],
    activeSessionId: 'A',
    messages: [] as ChatMessage[],
    selectedSiblings: {},
    streaming: null,
    queued: {},
    draft: '',
    loadMessages: realLoadMessages,
  });
});

describe('sendMessage — routes to the queue while streaming', () => {
  it('a send mid-turn makes no POST and grows the session queue', async () => {
    const p = openTurn();
    expect(completionPosts()).toHaveLength(1);
    await useChatStore.getState().sendMessage('first');
    await useChatStore.getState().sendMessage('second');
    expect(completionPosts()).toHaveLength(1);
    expect(useChatStore.getState().queued['A']).toEqual(['first', 'second']);
    frame(TURN_CLOSED);
    await p;
  });

  it('enqueue is per-session — chat A chips never surface inside chat B', async () => {
    const p = openTurn();
    await useChatStore.getState().sendMessage('a1');
    useChatStore.setState({ activeSessionId: 'B' });
    await useChatStore.getState().sendMessage('b1');
    expect(useChatStore.getState().queued).toEqual({ A: ['a1'], B: ['b1'] });
    useChatStore.setState({ activeSessionId: 'A' });
    useChatStore.getState().clearQueued('A');
    frame(TURN_CLOSED);
    await p;
  });

  it('a send with the chat registered but the slot null (the return window) still enqueues', async () => {
    const p = openTurn();
    // The return window: the slot is gone (setActiveSession cleared it) while
    // the registration stands until the reload re-adopts — a POST here would
    // hit the chat's own turn lock (409).
    useChatStore.setState({ streaming: null });
    await useChatStore.getState().sendMessage('window send');
    expect(completionPosts()).toHaveLength(1);
    expect(useChatStore.getState().queued['A']).toEqual(['window send']);
    frame(TURN_CLOSED);
    await p;
    useChatStore.getState().clearQueued('A');
  });
});

describe('clean turn-end — auto-flushes ONE coalesced message', () => {
  it('a clean terminal POSTs once with the joined text', async () => {
    const p = openTurn();
    await useChatStore.getState().sendMessage('part one');
    await useChatStore.getState().sendMessage('  ');
    await useChatStore.getState().sendMessage('part two');
    frame({ type: 'done', content: 'answer' });
    frame(TURN_CLOSED);
    await p;
    await tick();
    expect(completionPosts()).toHaveLength(2);
    expect(lastUserContent()).toBe('part one\n\npart two');
    expect(useChatStore.getState().queued['A']).toBeUndefined();
  });

  it('`done` alone does NOT flush — only turn_closed POSTs the follow-up', async () => {
    const p = openTurn();
    await useChatStore.getState().sendMessage('queued while streaming');
    frame({ type: 'done', content: 'answer' });
    await tick();
    await tick();
    // `done` is a content frame minted BEFORE the backend's finalize + lock
    // release: the turn is still open and the chips still stand.
    expect(completionPosts()).toHaveLength(1);
    expect(useChatStore.getState().queued['A']).toEqual(['queued while streaming']);
    frame(TURN_CLOSED);
    await p;
    await tick();
    await tick();
    expect(completionPosts()).toHaveLength(2);
    expect(lastUserContent()).toBe('queued while streaming');
    expect(useChatStore.getState().queued['A']).toBeUndefined();
  });

  it('done → turn_closed → the flushed follow-up receives its own frames (no terminal mis-route)', async () => {
    const p = openTurn();
    await useChatStore.getState().sendMessage('follow-up');
    frame({ type: 'done', content: 'answer' });
    // Give a `done`-triggered flush its macrotask: were `done` the terminal,
    // the follow-up would POST and register here, and the turn_closed below
    // would close THAT registration and drop its frames.
    await tick();
    await tick();
    frame(TURN_CLOSED);
    await p;
    await tick();
    await tick();
    expect(completionPosts()).toHaveLength(2);
    // The follow-up turn is REGISTERED: its ids lands in its own slot — the
    // previous turn's terminal already fired (it triggered the flush), so no
    // stale terminal can close this registration and drop its frames.
    frame({ type: 'ids', user_message_id: 'um2', assistant_message_id: 'am2' });
    expect(useChatStore.getState().streaming?.messageId).toBe('am2');
    frame({ type: 'delta', content: 'follow-up reply' });
    expect(useChatStore.getState().streaming?.messageId).toBe('am2');
  });

  it("the drained send's NEW streaming slot survives the old turn's runCompletion finally", async () => {
    const p = openTurn();
    await useChatStore.getState().sendMessage('follow-up');
    frame(TURN_CLOSED);
    await p;
    await tick();
    await tick();
    expect(completionPosts()).toHaveLength(2);
    // The follow-up turn is open: its slot and registration stand.
    expect(useChatStore.getState().streaming).not.toBeNull();
    frame({ type: 'ids', user_message_id: 'um2', assistant_message_id: 'am2' });
    expect(useChatStore.getState().streaming?.messageId).toBe('am2');
  });

  it('does nothing when the queue is empty', async () => {
    const p = openTurn();
    frame(TURN_CLOSED);
    await p;
    await tick();
    expect(completionPosts()).toHaveLength(1);
  });
});

describe('abort / error / halted / lost — restore the text to the composer', () => {
  async function endsWithRestore(end: () => void, sendPromise: Promise<void>) {
    end();
    await sendPromise.catch(() => {});
    await tick();
    expect(completionPosts()).toHaveLength(1);
    expect(useChatStore.getState().queued['A']).toBeUndefined();
    expect(useChatStore.getState().draft).toBe('saved one\n\nsaved two');
  }
  async function queueTwo() {
    await useChatStore.getState().sendMessage('saved one');
    await useChatStore.getState().sendMessage('saved two');
  }

  it('an error frame restores', async () => {
    const p = openTurn();
    await queueTwo();
    await endsWithRestore(() => {
      frame({ type: 'error', message: 'boom' });
      frame(TURN_CLOSED);
    }, p);
  });

  it('a lore/halt frame restores (a halt never auto-fires)', async () => {
    const p = openTurn();
    await queueTwo();
    await endsWithRestore(() => {
      frame({ type: 'lore/halt', seq: 5, data: { reason: 'tool_call_limit', turn: 0 } });
      frame(TURN_CLOSED);
    }, p);
  });

  it('Stop restores', async () => {
    const p = openTurn();
    await queueTwo();
    await endsWithRestore(() => {
      useChatStore.getState().stopGeneration();
      frame(TURN_CLOSED);
    }, p);
  });

  it('a gap-close (the terminal died with the socket) restores', async () => {
    const p = openTurn();
    await queueTwo();
    await endsWithRestore(() => {
      adoptOpenTurn(useChatStore.getState, useChatStore.setState, 'A', []);
    }, p);
  });

  it('a POST that fails before the turn opens restores', async () => {
    post.mockRejectedValueOnce(new Error('500'));
    const p = useChatStore.getState().sendMessage('kickoff');
    // The POST is in flight: the slot is claimed, so these queue.
    await queueTwo();
    await p.catch(() => {});
    await tick();
    expect(completionPosts()).toHaveLength(1);
    expect(useChatStore.getState().queued['A']).toBeUndefined();
    // The failed kickoff restores its OWN text first (sendMessage's
    // restore-on-fail), then the queue's error restore appends after it.
    expect(useChatStore.getState().draft).toBe('kickoff\n\nsaved one\n\nsaved two');
  });

  it('restore appends to text typed since queueing, never overwrites it', async () => {
    const p = openTurn();
    await queueTwo();
    useChatStore.setState({ draft: 'typed later' });
    frame({ type: 'error', message: 'boom' });
    frame(TURN_CLOSED);
    await p;
    await tick();
    expect(useChatStore.getState().draft).toBe('typed later\n\nsaved one\n\nsaved two');
  });
});

describe('flush is session-owned', () => {
  it('a non-active owner session is switched to, then sent', async () => {
    const loadMessages = vi.fn(async () => {});
    useChatStore.setState({
      sessions: [sess('A'), sess('B')],
      activeSessionId: 'B',
      queued: { A: ['cross-session'] },
      loadMessages,
    });
    // The flushed send is awaited to its turn's END — close it to settle.
    const p = useChatStore.getState().flushQueued('A');
    await tick();
    expect(useChatStore.getState().activeSessionId).toBe('A');
    expect(loadMessages).toHaveBeenCalledWith('A');
    expect(completionPosts()[0].url).toBe('/chat/sessions/A/completions');
    expect(lastUserContent()).toBe('cross-session');
    expect(useChatStore.getState().queued['A']).toBeUndefined();
    frame(TURN_CLOSED);
    await p;
  });

  it('a deleted owner session drops the queue and toasts chatQueueDropped', async () => {
    useChatStore.setState({ sessions: [sess('B')], activeSessionId: 'B', queued: { A: ['orphan'] } });
    await useChatStore.getState().flushQueued('A');
    expect(useChatStore.getState().queued['A']).toBeUndefined();
    expect(showToast()).toHaveBeenCalledWith('chatQueueDropped', 'info');
    expect(completionPosts()).toHaveLength(0);
  });
});

describe('a refused flush drains on the next load of its chat', () => {
  it('chips of A left while B streams fire when A is opened with no open turn', async () => {
    useChatStore.setState({ sessions: [sess('A'), sess('B')] });
    // A streams; a chip queues in A.
    const pA = openTurn('A');
    await useChatStore.getState().sendMessage('late follow-up');
    // The user switches to B (slot dropped) and B runs its own turn.
    useChatStore.setState({ activeSessionId: 'B', streaming: null });
    const pB = useChatStore.getState().sendMessage('b kickoff');
    frame({ type: 'ids', user_message_id: 'umB', assistant_message_id: 'amB' }, 'B');

    // A's turn ends clean while B is shown: the flush is refused (the shown
    // chat has an open turn) — the chips stay as chips.
    frame(TURN_CLOSED, 'A');
    await pA;
    await tick();
    expect(useChatStore.getState().queued['A']).toEqual(['late follow-up']);
    expect(completionPosts()).toHaveLength(2);

    // Opening A loads its rows (no open turn) — the chips fire there.
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      { message_id: 'am', chat_id: 'A', parent_id: 'um', role: 'assistant', content: 'answer', created_at: 't' },
    ]);
    useChatStore.getState().setActiveSession('A');
    await vi.waitFor(() => { if (completionPosts().length < 3) throw new Error('drain not fired'); });
    expect(lastUserContent()).toBe('late follow-up');
    expect(useChatStore.getState().queued['A']).toBeUndefined();

    frame(TURN_CLOSED, 'A');
    frame(TURN_CLOSED, 'B');
    await pB;
  });
});

describe('a chat reset forgets how turns ended', () => {
  it('a clean end recorded before the reset never fires chips after it', async () => {
    const flushQueued = vi.fn(async () => {});
    let state = { queued: {} as Record<string, string[]>, flushQueued, restoreQueued: vi.fn() };
    const get = () => state as unknown as ChatState;
    // A clean end of A is recorded (no chips at that moment).
    scheduleTurnEndFlush(get, 'A', 'done');
    // Logout / project switch: every module cache is cleared.
    clearChatCaches();
    // Chips of A exist on the next load (a new session of the same tab).
    state = { ...state, queued: { A: ['stale'] } };
    drainQueuedOnLoad(get, 'A');
    await tick();
    expect(flushQueued).not.toHaveBeenCalled();
  });
});

describe('a failed send keeps its text (restore-on-fail)', () => {
  it('a flush whose POST rejects restores the joined text, empties the chips, and toasts', async () => {
    const p = openTurn();
    await useChatStore.getState().sendMessage('queued one');
    await useChatStore.getState().sendMessage('queued two');
    frame(TURN_CLOSED);
    await p;
    post.mockRejectedValueOnce(new HttpError(409));
    await tick();
    await tick();
    await tick();
    // The chips cleared at flush time; the joined text is back in the composer.
    expect(useChatStore.getState().queued['A']).toBeUndefined();
    expect(useChatStore.getState().draft).toBe('queued one\n\nqueued two');
    expect(showToast()).toHaveBeenCalledWith('chatSendFailed', 'error');
    expect(completionPosts()).toHaveLength(2);
  });

  it('a manual send whose POST rejects restores its text after text typed meanwhile', async () => {
    post.mockRejectedValueOnce(new Error('500'));
    const p = useChatStore.getState().sendMessage('manual text');
    useChatStore.setState({ draft: 'typed meanwhile' });
    await p.catch(() => {});
    expect(useChatStore.getState().draft).toBe('typed meanwhile\n\nmanual text');
  });

  it('a send that fails after the user switched chats returns as a chip of its OWN chat', async () => {
    let reject!: (e: unknown) => void;
    post.mockImplementationOnce(() => new Promise((_, r) => { reject = r; }));
    useChatStore.setState({ sessions: [sess('A'), sess('B')] });
    const p = useChatStore.getState().sendMessage('meant for A');
    useChatStore.setState({ activeSessionId: 'B', draft: 'typed in B' });
    reject(new Error('500'));
    await p.catch(() => {});
    // The shared composer now belongs to B: the text must not land there.
    expect(useChatStore.getState().draft).toBe('typed in B');
    expect(useChatStore.getState().queued['A']).toEqual(['meant for A']);
  });

  it('an aborted send does NOT restore (a deliberate stop keeps the composer)', async () => {
    const err = new Error('aborted');
    err.name = 'AbortError';
    post.mockRejectedValueOnce(err);
    const p = useChatStore.getState().sendMessage('stopped text');
    await p.catch(() => {});
    expect(useChatStore.getState().draft).toBe('');
  });
});

describe('removeQueued', () => {
  it('removes one chip by index', () => {
    useChatStore.setState({ queued: { A: ['x', 'y', 'z'] } });
    useChatStore.getState().removeQueued('A', 1);
    expect(useChatStore.getState().queued['A']).toEqual(['x', 'z']);
  });
});
