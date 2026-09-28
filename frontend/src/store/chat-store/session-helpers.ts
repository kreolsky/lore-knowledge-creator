/** Shared helpers for chat-session insertion and pinning. */
import type { ChatSession } from '../../types';
import { useUIStore } from '../ui-store';
import type { Set } from './types';

export function insertAndPinSession(
  set: Set,
  session: ChatSession,
  opts: {
    focus?: boolean;
  },
): void {
  set(s => ({
    sessions: [session, ...s.sessions],
    activeSessionId: session.session_id,
    messages: [],
    selectedSiblings: {},
    pendingInputFocus: opts.focus !== false,
  }));
  // ARCH: the active chat is PROJECT-SCOPED.
  // A newly created chat becomes the project's last-active (survives document
  // navigation + reload). No per-document memory.
  useUIStore.getState().setLastActiveChatSession(session.session_id);
}
