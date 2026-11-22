/** A chat's new parent onto the store's session rows — the ONE reconcile for
 * both writers of the parent: the PATCH reply (`updateSession`) and the
 * server's `session_parent` chat frame (`dispatchChatFrame`: the agent's
 * move_chat, or the header picker in another of the user's tabs).
 *
 * ARCH: a reparent is written by the server to EVERY row of the conversation
 * (chat_sessions.update._propagate_parent), so the store follows for the whole
 * thread — a branch row left on the old parent splits the thread between two
 * documents until the next list reload. The addressed row takes the server's
 * row; every other row takes the five PARENT fields and nothing else — copying
 * document_id alone left a reference chat's siblings on a stale reference_id.
 */
import type { ChatSession } from '../../types';
import type { Set } from './types';

export function applySessionReparent(
  set: Set, updated: ChatSession, threadIds: ReadonlySet<string>,
): void {
  const parent = {
    document_id: updated.document_id ?? null,
    reference_id: updated.reference_id ?? null,
    document_title: updated.document_title ?? null,
    reference_title: updated.reference_title ?? null,
    host_document_id: updated.host_document_id ?? null,
  };
  set(s => ({
    sessions: s.sessions.map(ss => {
      if (ss.session_id === updated.session_id) {
        // WHY: update_session returns
        // last_message_at=null (it doesn't carry the last-activity map), so
        // preserve the in-memory value rather than discarding it — otherwise
        // a settings edit (which bumps updated_at server-side) would revert
        // the card to the patch time and re-sort the chat to the top by patch
        // time until the next list_sessions reload.
        return { ...updated, last_message_at: updated.last_message_at ?? ss.last_message_at };
      }
      return threadIds.has(ss.session_id) ? { ...ss, ...parent } : ss;
    }),
  }));
}

/** The `session_parent` chat frame — `{session, session_ids}` from the server
 * (the addressed row and every row of the conversation it walked). It arrives
 * as `unknown`: a malformed one is dropped with a console line and never
 * applied partially — the next list load reads the row's truth. */
export function applySessionParentFrame(set: Set, frame: unknown): void {
  const ev = frame as { session?: ChatSession; session_ids?: unknown } | null;
  const ids = ev?.session_ids;
  if (
    typeof ev?.session?.session_id !== 'string'
    || !Array.isArray(ids)
    || !ids.every(i => typeof i === 'string')
  ) {
    console.warn('Malformed chat frame:', frame);
    return;
  }
  applySessionReparent(set, ev.session, new Set(ids));
}
