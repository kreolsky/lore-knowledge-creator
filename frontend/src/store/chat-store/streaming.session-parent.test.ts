/** dispatchChatFrame's `session_parent` arm — a chat's new parent, pushed by the
 * server for ANY of the user's chats (the agent's move_chat, the header picker
 * in another tab). Pinned here: a frame for a chat that is NOT active and has
 * NO registered turn still moves its rows — the arm sits ahead of the no-turn
 * drop — and copies all five parent fields onto every row the server walked;
 * a chat outside `session_ids` stays untouched.
 */
import { describe, it, expect } from 'vitest';
import { dispatchChatFrame } from './streaming';
import type { ChatSession } from '../../types';
import type { ChatState, Set } from './types';

function row(session_id: string, fields: Partial<ChatSession> = {}): ChatSession {
  return {
    session_id, project_id: 'p1', user_id: 'u1', title: session_id, model: 'm',
    system_prompt_id: null, document_id: null, reference_id: null,
    updated_at: '2026-01-01T00:00:00Z', created_at: '2026-01-01T00:00:00Z',
    ...fields,
  };
}

function makeStore(sessions: ChatSession[], activeSessionId: string | null) {
  let state = {
    sessions, activeSessionId, messages: [], streaming: null, inboxQueue: [],
  } as unknown as ChatState;
  const get = () => state;
  const set: Set = u => {
    const patch = typeof u === 'function' ? (u as (s: ChatState) => Partial<ChatState>)(state) : u;
    state = { ...state, ...(patch as Partial<ChatState>) };
  };
  return { get, set };
}

describe('dispatchChatFrame — session_parent (a moved chat)', () => {
  // TEST-WHY: pure-logic-edge — the frame moves a NON-active chat that has no registered turn; an arm placed behind the no-turn drop would lose the move silently
  it('moves a non-active chat with no turn: its row takes the server row, the walked rows take the five parent fields, the rest stay', () => {
    const old = {
      document_id: 'd-old', reference_id: null, document_title: 'Old',
      reference_title: null, host_document_id: 'd-old',
    };
    const { get, set } = makeStore([
      row('active', { document_id: 'd-active', document_title: 'Active' }),
      row('r1', { ...old, thread_id: 'r1', last_message_at: '2026-02-02T00:00:00Z' }),
      row('b1', { ...old, thread_id: 'r1' }),
      row('x1', { ...old, thread_id: 'x1' }),
    ], 'active');
    const moved: ChatSession = {
      ...row('r1'), document_id: 'd-new', reference_id: 'ref-9', document_title: null,
      reference_title: 'Ref 9', host_document_id: 'd-new',
    };

    dispatchChatFrame(get, set, 'r1', { type: 'session_parent', session: moved, session_ids: ['b1', 'r1'] });

    const byId = Object.fromEntries(get().sessions.map(s => [s.session_id, s]));
    // The addressed row is the server's row, with the in-memory last_message_at kept.
    expect(byId.r1).toMatchObject({
      document_id: 'd-new', reference_id: 'ref-9', document_title: null,
      reference_title: 'Ref 9', host_document_id: 'd-new', last_message_at: '2026-02-02T00:00:00Z',
    });
    // A sibling of the thread: the five parent fields, and nothing else of it changed.
    expect(byId.b1).toMatchObject({
      document_id: 'd-new', reference_id: 'ref-9', document_title: null,
      reference_title: 'Ref 9', host_document_id: 'd-new', title: 'b1',
    });
    // Another chat and the active chat are not part of the move.
    expect(byId.x1).toMatchObject(old);
    expect(byId.active).toMatchObject({ document_id: 'd-active', document_title: 'Active' });
    expect(get().activeSessionId).toBe('active');
  });
});
