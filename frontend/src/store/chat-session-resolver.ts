/**
 * Pure resolver for active-chat-session lifecycle.
 *
 * // SYSTEM: chat-session-resolver — discriminated union encoding session lifecycle decisions
 * // ARCH: Replaces nested branches in chat-store.loadSessions. The variants map to the
 * //       ghost-chat lifecycle: a project with no restorable chat yields a client-only
 * //       ghost (`none` → activeSessionId = null). Materialization (a real DB row)
 * //       happens ONLY on the first sent message via ChatInput's lazy-create path.
 *
 * // INVARIANT: The ACTIVE chat is PROJECT-SCOPED.
 * // Why: one project ↔ one last-active chat (ui-store), surviving document navigation — switching docs must not re-resolve or clobber the project's chat pointer.
 * //            One project remembers ONE last-active chat (ui-store
 * //            lastActiveChatSessionId). It is matched against the ENTIRE
 * //            project-wide list — ownership (session.document_id) is NOT a filter.
 * //            The chat survives document navigation: switching documents does NOT
 * //            re-resolve (ChatPanel reloads on PROJECT change only). A project
 * //            with no last-active id (first open / value cleared / id deleted)
 * //            yields `none` (ghost) — there is NO auto-adoption of any chat.
 * //            Materialization happens on first send.
 */

import type { ChatSession } from '../types';

export type SessionResolution =
  | { action: 'keep_current'; sessionId: string }
  | { action: 'restore_saved'; sessionId: string }
  | { action: 'none' };

export interface ResolveParams {
  sessions: ChatSession[];
  currentActiveId: string | null;
  // Project-level last-active chat (ui-store lastActiveChatSessionId). The
  // restore source. Matched against the whole project-wide list — a chat's
  // owning document is not a filter.
  lastActiveSessionId: string | null;
}

export function resolveActiveSession(params: ResolveParams): SessionResolution {
  const { sessions, currentActiveId, lastActiveSessionId } = params;

  // keep_current: the active session survived the (re)load and is still present
  // in the project-wide list — e.g. a re-fetch within the same project
  // (openChatWithReference). No reload of messages needed.
  if (currentActiveId && sessions.some(s => s.session_id === currentActiveId)) {
    return { action: 'keep_current', sessionId: currentActiveId };
  }

  // Restore the project's last-active chat whenever it still exists in the
  // project-wide list — ownership is not a filter. A deleted id (or a project
  // with no last-active pointer) falls through to `none`: never auto-adopt.
  if (lastActiveSessionId && sessions.some(s => s.session_id === lastActiveSessionId)) {
    return { action: 'restore_saved', sessionId: lastActiveSessionId };
  }
  return { action: 'none' };
}
