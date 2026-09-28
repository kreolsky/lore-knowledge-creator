/** Single-responsibility module for chat context (documents + references added to LLM system prefix). */
// SYSTEM: chat-context — per-session document/reference context with server sync + rollback
// ARCH: Internal Zustand store for React reactivity. No context state leaked to ui-store or chat-store.
// ARCH: Does NOT import chat-store — avoids circular dependency (chat-store imports context.ts).
//       Access to activeSessionId is wired via setupChatContextBridge().
// INVARIANT: Always arrays — never singular fields. Context is strictly opt-in: nothing is sent unless explicitly selected.  Why: the contract is plural + opt-in — every field is an array populated only on explicit selection, so an unset context never sends a default/singular value the model would misread as a real reference.

import { create } from 'zustand';
import { apiClient } from '../api/client';
import type { ChatSession } from '../types';
import { addWithCascade, subtractWithCascade, type FirstCircle } from '../utils/cascade-selection';
import { fetchDocumentLinksFresh, fetchReferenceLinksFresh, linkCache } from '../api/links';
import { splitContextIds } from './session-helpers';
// Register this module's pending-patch clear on the chat-reset registry. This is
// a direct import of the standalone reset-registry leaf (which imports nothing), so
// it does NOT violate the "does not import chat-store" cycle invariant above.
import { registerChatResetHandler } from '../store/chat-store/reset-registry';
// Ghost derivation lives in ./ghost-context (its own SYSTEM marker there). Imported here only
// for getDerivedGhostContext (the materialization snapshot), which bridges the
// app-store/ui state to the pure ghost selector.
import { deriveGhostContext, getGhostDeltas } from './ghost-context';

export interface ChatContext {
  documentIds: string[];
  referenceIds: string[];
}

export interface CompletionContext {
  context_ids: string[];
}

interface ContextStoreState {
  contextBySession: Record<string, ChatContext>;
}

const useInternalContextStore = create<ContextStoreState>(() => ({
  contextBySession: {},
}));

let _updateChatSessionInStore: ((sessionId: string, session: ChatSession) => void) | null = null;
let _appBridge: {
  showToast: (msg: string, type: 'info' | 'error') => void;
} | null = null;
let _getActiveSessionId: (() => string | null) | null = null;
let _getReferenceIdSet: (() => Set<string>) | null = null;

interface AppStoreBridge {
  getState: () => { currentDocument: { document_id: string } | null; currentReference: { reference_id: string } | null };
}

let _appStore: AppStoreBridge | null = null;
let _getSplitView: ((docId: string) => boolean) | null = null;

// INVARIANT: bridge consumers must fail loud if unwired. Why: silent no-op sends
// empty LLM context. _bridgeReady flips to true only at the end of
// setupChatContextBridge; resetChatContextBridge flips it back (test-only).
let _bridgeReady = false;

export function setupChatContextBridge(opts: {
  updateChatSession: (sessionId: string, session: ChatSession) => void;
  showToast: (msg: string, type: 'info' | 'error') => void;
  getActiveSessionId: () => string | null;
  getAppStoreState: () => { currentDocument: { document_id: string } | null; currentReference: { reference_id: string } | null };
  getSplitView: (docId: string) => boolean;
  getReferenceIdSet: () => Set<string>;
}) {
  _updateChatSessionInStore = opts.updateChatSession;
  _appBridge = { showToast: opts.showToast };
  _getActiveSessionId = opts.getActiveSessionId;
  _appStore = { getState: opts.getAppStoreState };
  _getSplitView = opts.getSplitView;
  _getReferenceIdSet = opts.getReferenceIdSet;
  _bridgeReady = true;
}

/** Reset the bridge to its unwired state. Test-only — never call from app code. */
export function resetChatContextBridge() {
  _updateChatSessionInStore = null;
  _appBridge = null;
  _getActiveSessionId = null;
  _appStore = null;
  _getSplitView = null;
  _getReferenceIdSet = null;
  _bridgeReady = false;
}

function reportUnwiredBridge(op: string) {
  _appBridge?.showToast('Chat context not initialized', 'error');
  console.error(`[chat-context] ${op} invoked before setupChatContextBridge — bridge is unwired.`);
}

const EMPTY_CONTEXT: ChatContext = Object.freeze({ documentIds: [], referenceIds: [] });

// GHOST_SESSION_ID is the client-only sentinel used as the stored-bucket branch key by
// the materialized-session context path below (setContextForSession / hydrateFromSessions
// skip the server PATCH / prune for it). The ghost DERIVATION model lives in ./ghost-context
// (see ./ghost-context); this module snapshots it onto a real id at createSession.
export const GHOST_SESSION_ID = '__ghost__';

// WHY: Debounce PATCH calls to avoid flurry of requests during rapid context
// changes (content picker toggle, document/reference switches). Optimistic
// Zustand update is instant; only the server-sync is delayed by 200ms.
const DEBOUNCE_MS = 200;
const _pendingPatches = new Map<string, ReturnType<typeof setTimeout>>();
const _pendingValues = new Map<string, { docIds: string[]; refIds: string[]; prevDocIds: string[]; prevRefIds: string[] }>();

// INVARIANT: a first-circle cascade that started before its session was RESET
// (clearContextForSession) must not merge its late-arriving links back in.  Why: a cascade started before the reset can resolve after it; merging late links would re-pollute the cleared context, so reset sessions are excluded from applyCascade.
// Why (post chat-context-derived-refactor): applyCascade now serves ONLY materialized
// sessions — the ghost uses deltas, not this path. clearContextForSession is still
// called on a materialized session being DELETED (sessions-slice.deleteSession). If a
// picker cascade is in flight when the chat is deleted, the late merge must NOT
// re-create the bucket and PATCH a deleted session (→ 404 → spurious "Failed to save
// chat context" toast). clearContextForSession bumps the epoch; applyCascade captures
// it before its await and drops the merge if the session was reset in between.
// Interleaved adds WITHOUT a reset keep the "reads current state" merge (no epoch
// change). Pinned by the "cascade whose session is deleted mid-fetch" test.
const _cascadeEpoch = new Map<string, number>();

function flushPatch(sessionId: string) {
  const val = _pendingValues.get(sessionId);
  if (!val) return;
  _pendingPatches.delete(sessionId);
  _pendingValues.delete(sessionId);

  // INVARIANT: PATCH flush must not run while the bridge is unwired — the
  // success handler would silently no-op (_updateChatSessionInStore is null),
  // leaving the store out of sync with the server. Fail loud instead.  Why: unwired, the success handler's store-update fn is null — a 'successful' flush would silently no-op and the store would drift from the server; failing loud surfaces the wiring bug.
  if (!_bridgeReady) {
    reportUnwiredBridge('flushPatch');
    return;
  }

  apiClient.patch(`/chat/sessions/${sessionId}`, {
    context_ids: [...val.docIds, ...val.refIds],
  })
    .then(updated => _updateChatSessionInStore?.(sessionId, updated as ChatSession))
    .catch(() => {
      useInternalContextStore.setState(s => ({
        contextBySession: { ...s.contextBySession, [sessionId]: { documentIds: val.prevDocIds, referenceIds: val.prevRefIds } },
      }));
      _appBridge?.showToast('Failed to save chat context', 'error');
    });
}

export function useChatContext(sessionId: string | null): ChatContext {
  return useInternalContextStore(s =>
    sessionId ? (s.contextBySession[sessionId] ?? EMPTY_CONTEXT) : EMPTY_CONTEXT,
  );
}

export function getContextForSession(sessionId: string): ChatContext {
  return useInternalContextStore.getState().contextBySession[sessionId] ?? { documentIds: [], referenceIds: [] };
}

export function setContextForSession(sessionId: string, docIds: string[], refIds: string[]) {
  const prev = getContextForSession(sessionId);

  useInternalContextStore.setState(s => ({
    contextBySession: { ...s.contextBySession, [sessionId]: { documentIds: docIds, referenceIds: refIds } },
  }));

  // INVARIANT: the ghost sentinel is client-only — there is no DB row to PATCH.  Why: the ghost id has no server row, so PATCH /chat/sessions/__ghost__ would 404 → 'Failed to save chat context' toast + revert; the flush is skipped for it.
  // Why skip PATCH for GHOST_SESSION_ID: scheduling flushPatch('__ghost__') would
  // fire PATCH /chat/sessions/__ghost__ → 404 → "Failed to save chat context"
  // toast + revert. The ghost context is DERIVED (never written to this bucket in
  // the new model); materialization snapshots it onto a real id via
  // getDerivedGhostContext → setContextForSession(realId).
  if (sessionId === GHOST_SESSION_ID) return;

  _pendingValues.set(sessionId, { docIds, refIds, prevDocIds: prev.documentIds, prevRefIds: prev.referenceIds });
  const existing = _pendingPatches.get(sessionId);
  if (existing) clearTimeout(existing);
  _pendingPatches.set(sessionId, setTimeout(() => flushPatch(sessionId), DEBOUNCE_MS));
}

// ARCH: Single entry point for cascade add/remove of items in chat context.
//
// All chat-context mutations from every entry point — picker checkboxes,
// session auto-populate on creation, "Chat with Reference" — go through this
// function. Three properties make the pipeline stable:
//   (1) Idempotent: calling add(X) twice is the same as calling once. Callers
//       at different layers can both invoke it without coordination.
//   (2) Symmetric: add and remove use the same primitive (applyCascade with
//       mode) and the same helpers (addWithCascade / subtractWithCascade).
//       Removing X always removes X and X's first circle, regardless of how
//       any individual id landed in the selection. There is NO opt-out memory.
//   (3) Self-fetching: the first-circle fetch happens inside this function on
//       demand. Callers never need to pre-warm a cache; correctness does not
//       depend on cache state.
//
// Sync step persists the target id immediately so the UI flips instantly;
// async step fetches the first-circle and re-applies on top of the CURRENT
// context state (re-read after await — never snapshotted, so overlapping
// add/remove ops on the same session don't clobber each other). The 200ms
// PATCH debounce in setContextForSession coalesces both writes into one
// network call when the fetch is fast.
async function applyCascade(
  sessionId: string,
  kind: 'doc' | 'ref',
  id: string,
  mode: 'add' | 'subtract',
): Promise<void> {
  // INVARIANT: cascade must read the FRESH first-circle, never the warm cache.
  // Why: the cache is not durably invalidated, so a stale entry over-/under-adds
  // linked entities relative to the document's real current content.
  const fetcher = kind === 'doc' ? fetchDocumentLinksFresh : fetchReferenceLinksFresh;

  // Capture the reset epoch BEFORE the await; if clearContextForSession bumps it
  // while the fetch is in flight, this cascade's base was superseded → skip the
  // merge (see the _cascadeEpoch INVARIANT).
  const epoch = _cascadeEpoch.get(sessionId) ?? 0;

  // 1) Sync target-only update for instant feedback.
  const before = getContextForSession(sessionId);
  const syncDocs = new Set(before.documentIds);
  const syncRefs = new Set(before.referenceIds);
  if (mode === 'add') {
    if (kind === 'doc') syncDocs.add(id); else syncRefs.add(id);
  } else {
    if (kind === 'doc') syncDocs.delete(id); else syncRefs.delete(id);
  }
  setContextForSession(sessionId, [...syncDocs], [...syncRefs]);

  // 2) Async first-circle expansion. We deliberately re-read context after the
  // await so an interleaved op on the same session is preserved.
  let links: FirstCircle;
  try {
    links = await fetcher(id);
  } catch {
    _appBridge?.showToast('Failed to load links', 'error');
    return;
  }
  // The session was reset (e.g. ghost re-attached to a different open entity) while
  // this fetch was in flight — its links are stale; do not merge them back in.
  if ((_cascadeEpoch.get(sessionId) ?? 0) !== epoch) return;
  const current = getContextForSession(sessionId);
  const selection = { docIds: current.documentIds, refIds: current.referenceIds };
  const next = mode === 'add'
    ? addWithCascade(selection, kind, id, links)
    : subtractWithCascade(selection, kind, id, links);
  setContextForSession(sessionId, next.docIds, next.refIds);
}

/**
 * Add `id` to session context and cascade-include its first-circle links.
 * Used by:
 *   - ContentPickerPopup.toggleItem (manual check)
 *   - chat-store.createSession (auto-populate currentDoc/currentRef)
 *   - chat-store.openChatWithReference (post-hydrate add, serialized)
 */
export function addItemToContext(sessionId: string, kind: 'doc' | 'ref', id: string): Promise<void> {
  return applyCascade(sessionId, kind, id, 'add');
}

/**
 * Remove `id` from session context and cascade-remove its first-circle links.
 * Used by: ContentPickerPopup.toggleItem (manual uncheck). Removing a child
 * does not touch its parent; removing a parent removes its full first circle
 * (children added by other parents or manually are removed too — no memory).
 */
export function removeItemFromContext(sessionId: string, kind: 'doc' | 'ref', id: string): Promise<void> {
  return applyCascade(sessionId, kind, id, 'subtract');
}

export function clearContextForSession(sessionId: string) {
  const timer = _pendingPatches.get(sessionId);
  if (timer) { clearTimeout(timer); _pendingPatches.delete(sessionId); }
  _pendingValues.delete(sessionId);
  // Invalidate any in-flight first-circle cascade for this session (see the
  // _cascadeEpoch INVARIANT) — a reset must drop links that have not yet merged.
  _cascadeEpoch.set(sessionId, (_cascadeEpoch.get(sessionId) ?? 0) + 1);

  useInternalContextStore.setState(s => {
    const next = { ...s.contextBySession };
    delete next[sessionId];
    return { contextBySession: next };
  });
}

export function clearPendingContextPatches(): void {
  for (const timer of _pendingPatches.values()) clearTimeout(timer);
  _pendingPatches.clear();
  _pendingValues.clear();
}

// self-register the pending-patch clear on the chat-reset registry.
registerChatResetHandler(clearPendingContextPatches);

// ARCH: Merge-based hydration (Spec §5).
// INVARIANT: Optimistic context overrides server snapshot until the in-flight PATCH resolves.
// A wholesale overwrite would clobber adds made between the GET and the PATCH-flush.  Why: the user can add context between the GET and the flush; the optimistic override preserves those in-flight adds until the PATCH resolves.
// Sessions absent from the response are pruned (e.g. deleted server-side), but a pending
// session is kept regardless — guards against PATCH-not-yet-flushed races.
// INVARIANT: hydrateFromSessions requires app-store references to be loaded
// first; otherwise ref ids misclassify as doc ids. Why: split is by refIdSet.
export function hydrateFromSessions(sessions: ChatSession[]) {
  const refIdSet = _getReferenceIdSet?.() ?? new Set<string>();

  // Observability guard: if the bridge is wired but the reference set is empty
  // while a session that relies on the LEGACY fallback (no server
  // context_reference_ids) carries context_ids, every id would be misclassified
  // as a doc id. Log a warning so the ordering drift is visible — do not crash
  // (the misclassification matches the historical "all-docs" fallback until
  // reload). Sessions carrying the server field classify correctly regardless,
  // so they are excluded from the warning (no false positive).
  if (_bridgeReady && refIdSet.size === 0 && sessions.some(s =>
    (s.context_ids ?? []).length > 0 && s.context_reference_ids === undefined,
  )) {
    console.warn(
      '[chat-context] hydrateFromSessions called with an empty reference id set while a session lacks server context_reference_ids — ' +
      'reference ids will be misclassified as documents until reload. Ensure app-store references are loaded before loadSessions.',
    );
  }

  useInternalContextStore.setState(s => {
    const merged: Record<string, ChatContext> = { ...s.contextBySession };
    const liveIds = new Set<string>();
    for (const session of sessions) {
      liveIds.add(session.session_id);
      if (_pendingPatches.has(session.session_id)) continue;
      // ARCH: prefer the
      // server-authored split. The open document's reference scope (refIdSet)
      // is NEVER evidence of a reference's existence — a cross-doc ref is
      // permanently outside that scope and the legacy split would flip it into
      // documentIds on every doc/chat switch ("partial reset"). Only the rare
      // older server (field absent) falls back to splitContextIds.
      let docIds: string[];
      let refIds: string[];
      if (session.context_reference_ids !== undefined) {
        const srvRefSet = new Set(session.context_reference_ids);
        refIds = session.context_reference_ids;
        docIds = (session.context_ids ?? []).filter(id => !srvRefSet.has(id));
      } else {
        ({ docIds, refIds } = splitContextIds(session.context_ids ?? [], refIdSet));
      }
      merged[session.session_id] = { documentIds: docIds, referenceIds: refIds };
    }
    for (const sid of Object.keys(merged)) {
      // The ghost sentinel is never a server session id and is never written to the
      // stored bucket anymore (the ghost context is DERIVED). It is neither live nor
      // pending-PATCH, so skip it on prune out of an abundance of caution — nothing
      // populates it, but a stray empty entry must not round-trip through a PATCH.
      if (sid === GHOST_SESSION_ID) continue;
      if (!liveIds.has(sid) && !_pendingPatches.has(sid)) delete merged[sid];
    }
    return { contextBySession: merged };
  });
}

export function pruneContext(sessionId: string, aliveDocIds: Set<string>, deletedRefIds: Set<string>) {
  const ctx = getContextForSession(sessionId);
  const dirtyDocs = ctx.documentIds.filter(id => !aliveDocIds.has(id));
  const dirtyRefs = ctx.referenceIds.filter(id => deletedRefIds.has(id));
  if (dirtyDocs.length > 0 || dirtyRefs.length > 0) {
    const cleanDocs = ctx.documentIds.filter(id => aliveDocIds.has(id));
    const cleanRefs = ctx.referenceIds.filter(id => !deletedRefIds.has(id));
    setContextForSession(sessionId, cleanDocs, cleanRefs);
  }
}

export interface ContextPruneDecision {
  aliveDocIds: Set<string>;
  deletedRefIds: Set<string>;
}

/**
 * Pure gate for the ChatInput prune effect: returns the prune inputs when
 * something should be pruned, or null when the effect must do NOTHING (must NOT
 * call pruneContext / setContextForSession — that would PATCH context away).
 *
 * INVARIANT (context-race): a context id is pruned only when the SERVER says
 * which ids are references (`context_reference_ids`); the open document's
 * reference scope is never evidence of a reference's existence. Why: cross-doc
 * refs are selectable (Part B) and are permanently outside that scope, so
 * scope-absence would PATCH away live selections. Rules:
 *   - `documents` empty (not loaded) → null (an empty list on first paint would
 *     prune the whole context).
 *   - `context_reference_ids` undefined (older server / not yet loaded) → null:
 *     the doc/ref split is untrusted, so doc pruning is skipped entirely rather
 *     than run against a scope-limited set. Reference pruning (deletedRefIds)
 *     is also deferred — transient staleness until reload, not data loss.
 *   - Otherwise prune docs against `documents` (the whole project) and refs
 *     against `deletedRefIds` only (never "absent from the open scope").
 */
export function computeContextPrune(
  ctx: ChatContext,
  context_reference_ids: string[] | undefined,
  documents: ReadonlyArray<{ document_id: string }>,
  deletedRefIds: Set<string>,
): ContextPruneDecision | null {
  if (documents.length === 0) return null;
  if (context_reference_ids === undefined) return null;
  const aliveDocIds = new Set(documents.map(d => d.document_id));
  const dirtyDocs = ctx.documentIds.filter(id => !aliveDocIds.has(id));
  const dirtyRefs = ctx.referenceIds.filter(id => deletedRefIds.has(id));
  if (dirtyDocs.length === 0 && dirtyRefs.length === 0) return null;
  return { aliveDocIds, deletedRefIds };
}

export function resolveCompletionContext(): CompletionContext {
  // INVARIANT: bridge consumers must fail loud if unwired. Why: a silent no-op
  // here sends an empty LLM context with no warning, violating no-silent-degradation.
  if (!_bridgeReady) {
    reportUnwiredBridge('resolveCompletionContext');
  }
  // The active session's context is its STORED snapshot (materialized at
  // createSession from deriveGhostContext, then user edits via PATCH+rollback).
  // There is NO open-entity fallback anymore: the ghost derives its own context,
  // and a materialized session keeps exactly the context it was sent with (a user
  // who cleared context sends with none). Why: the old empty-bucket fallback
  // re-derived from the open entity at send time, disagreeing with the picker —
  // the entire point of this refactor is one source of truth.
  const sessionId = _getActiveSessionId?.() ?? null;
  if (!sessionId) return { context_ids: [] };
  const ctx = getContextForSession(sessionId);
  return { context_ids: [...ctx.documentIds, ...ctx.referenceIds] };
}

/**
 * Synchronous materialization snapshot: derive the ghost context from the CURRENT
 * app/ui state (via the bridge) + linkCache + ghost deltas. Called inside
 * createSession BEFORE any further await so the first turn sees full context.
 * linkCache is warm by send time (the warm-effect fills it while the ghost shows).
 */
export function getDerivedGhostContext(): { docIds: string[]; refIds: string[] } {
  // INVARIANT: bridge consumers must fail loud if unwired. Why: a pre-wire call
  // (e.g. a module evaluating this before setupChatContextBridge) silently yields
  // an EMPTY snapshot at materialization — the first turn ships with no context
  // and no signal. Mirror resolveCompletionContext above: warn, then return a sane
  // empty result (do NOT continue into deriveGhostContext — with a null app/ui
  // bridge the result is empty anyway, and an early return avoids touching the
  // possibly-unmocked linkCache). Symmetric with the other bridge consumers; no
  // behavior change in the wired path.
  if (!_bridgeReady) {
    reportUnwiredBridge('getDerivedGhostContext');
    return { docIds: [], refIds: [] };
  }
  const appState = _appStore?.getState();
  const docId = appState?.currentDocument?.document_id ?? null;
  const refId = appState?.currentReference?.reference_id ?? null;
  const splitActive = !!(docId && _getSplitView?.(docId));
  return deriveGhostContext({ docId, refId, splitActive, linkCache, deltas: getGhostDeltas() });
}

// Re-export the ghost-context public surface from its new home so existing imports
// of these symbols from './context' keep working — vi.mock('../chat/context', …)
// factories and the dynamic import('../chat/context') in createSession.test.ts rely
// on this stable path.
export {
  type GhostDeltas,
  type GhostDeriveInput,
  type GhostDeltaState,
  ghostBaseTargets,
  deriveGhostContext,
  computeGhostBaseKey,
  useGhostDeltaSnapshot,
  getGhostDeltas,
  syncGhostBaseKey,
  addGhostDelta,
  removeGhostDelta,
  resetGhostDeltas,
  __resetGhostDeltaStore,
} from './ghost-context';
