import { describe, it, expect } from 'vitest';
import { resolveActiveSession } from './chat-session-resolver';
import type { ChatSession } from '../types';

const make = (id: string, doc: string | null, updated = '2026-01-01'): ChatSession => ({
  session_id: id,
  project_id: 'p1',
  document_id: doc,
  reference_id: null,
  user_id: 'u1',
  title: '',
  model: 'm',
  system_prompt_id: null,
  context_ids: [],
  created_at: updated,
  updated_at: updated,
});

// Base params shared by every case — only the diverging fields are overridden.
// The active chat is now PROJECT-SCOPED:
// resolution ignores which document owns a chat; the project remembers ONE
// last-active chat id (ui-store lastActiveChatSessionId).
const base = {
  currentActiveId: null,
  lastActiveSessionId: null,
};

describe('resolveActiveSession (project-scoped active chat)', () => {
  // ── keep_current ────────────────────────────────────────────────────────
  it('keep_current when the active session is still in the loaded list (re-fetch)', () => {
    // A re-fetch within the same project (e.g. openChatWithReference) where the
    // active chat survives keeps it — no reload of messages needed.
    const sessions = [make('a', 'd1'), make('b', 'd2')];
    const r = resolveActiveSession({ sessions, ...base, currentActiveId: 'a' });
    expect(r).toEqual({ action: 'keep_current', sessionId: 'a' });
  });

  // ── restore (project-level) ─────────────────────────────────────────────
  it('restores the project-level last-active chat when it is in the list', () => {
    // First open / project switch: currentActiveId is null (or from another
    // project and absent here). The project-level last-active id is restored.
    const sessions = [make('foreign', 'd2'), make('sib', 'd3')];
    const r = resolveActiveSession({ sessions, ...base, lastActiveSessionId: 'foreign' });
    expect(r).toEqual({ action: 'restore_saved', sessionId: 'foreign' });
  });

  it('restore is cross-document: ownership is not a filter', () => {
    // The project's last-active chat may be owned by any document — it restores
    // regardless of which document is currently open.
    const sessions = [make('owned-by-other', 'd9')];
    const r = resolveActiveSession({ sessions, ...base, lastActiveSessionId: 'owned-by-other' });
    expect(r).toEqual({ action: 'restore_saved', sessionId: 'owned-by-other' });
  });

  it('keep_current wins over restore when the active session survived', () => {
    const sessions = [make('a', 'd1'), make('b', 'd1')];
    const r = resolveActiveSession({
      sessions, ...base, currentActiveId: 'a', lastActiveSessionId: 'b',
    });
    expect(r).toEqual({ action: 'keep_current', sessionId: 'a' });
  });

  it('restores last-active when the previous active session is gone from the list', () => {
    // Project switch: the prior project's active chat is absent from the new
    // project-wide list → restore the new project's last-active id instead.
    const sessions = [make('b', 'd1')];
    const r = resolveActiveSession({
      sessions, ...base, currentActiveId: 'gone', lastActiveSessionId: 'b',
    });
    expect(r).toEqual({ action: 'restore_saved', sessionId: 'b' });
  });

  // ── none (ghost) ────────────────────────────────────────────────────────
  it('empty list yields none (ghost)', () => {
    const r = resolveActiveSession({ sessions: [], ...base });
    expect(r).toEqual({ action: 'none' });
  });

  it('project with no last-active chat yields none (ghost, never auto-adopts)', () => {
    // No auto-adoption: a project that has chats but no last-active pointer
    // (first open / value cleared) yields ghost, not an arbitrary owned chat.
    const sessions = [make('a', 'd1'), make('b', 'd1')];
    const r = resolveActiveSession({ sessions, ...base });
    expect(r).toEqual({ action: 'none' });
  });

  it('last-active chat absent from the list yields none', () => {
    // The last-active id was deleted (or belongs to a different project) → ghost.
    const sessions = [make('a', 'd1')];
    const r = resolveActiveSession({ sessions, ...base, lastActiveSessionId: 'deleted' });
    expect(r).toEqual({ action: 'none' });
  });

  it('empty inputs return none', () => {
    const r = resolveActiveSession({ sessions: [], ...base });
    expect(r).toEqual({ action: 'none' });
  });
});
