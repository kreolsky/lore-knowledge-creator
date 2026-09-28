/** Shared module-level state for chat-store slice coordination. */
import { registerChatResetHandler } from './reset-registry';

// INVARIANT: tracks the latest updateSession PATCH so loadSessions can flush
// before createSession — prevents the race where inheritance queries the DB
// before a prior PATCH (model / system_prompt_id) has committed.  Why: tracking the latest PATCH lets loadSessions flush it before createSession, so inheritance sees the latest model/system_prompt.
let _pendingSessionPatch: Promise<unknown> | null = null;

export function getPendingSessionPatch(): Promise<unknown> | null {
  return _pendingSessionPatch;
}

export function setPendingSessionPatch(p: Promise<unknown> | null): void {
  _pendingSessionPatch = p;
}

// ARCH: Shared in-flight promise map for loadSessions deduplication.
// Replaces the old boolean sessionsLoadingFor flag. When two callers
// (composite action + reactive useEffect) request the same scope, both
// await the same promise. No caller can proceed past a still-in-flight
// hydrate — the race class that broke "Chat with Reference" 3+ times
// is structurally impossible.
//
// INVARIANT: Entries are deleted once the promise settles (caller-side
// `.finally`). Concurrent callers in the same tick still dedupe via the
// in-flight entry, but a later visit to the same scope MUST re-fetch.  Why: entries are deleted on settle so a later visit re-fetches; same-tick callers dedupe via the in-flight entry, but keeping it would serve stale data.
// Without this, a docA → docB → docA navigation hits the resolved-docA
// entry and returns immediately, leaving state.sessions / activeSessionId
// stuck on docB. Re-running hydrateFromSessions is safe: it skips sessions
// that have a pending PATCH (see chat/context.ts:218).
export const loadInFlight = new Map<string, Promise<void>>();

export function clearInflight(): void {
  loadInFlight.clear();
  _pendingSessionPatch = null;
}

// self-register on the chat-reset registry (see tree.ts for rationale).
registerChatResetHandler(clearInflight);
