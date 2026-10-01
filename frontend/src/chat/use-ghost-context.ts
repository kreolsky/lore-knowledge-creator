/**
 * Reactive bindings for the DERIVED ghost context.
 *
 * context.ts stays a pure store module (no app/ui import, no React render hooks) —
 * these hooks wire it to the app/ui stores. The ghost (zero-chat) auto-context is a
 * pure function of the open entity + first-circle + manual deltas; it is recomputed
 * on every read and NEVER stored.
 */

import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { readRefOpenMode, showsBothPanes, refIsScope } from '../store/ui-store/documents-slice';
import { useChatStore } from '../store/chat-store';
import {
  deriveGhostContext,
  ghostBaseTargets,
  useGhostDeltaSnapshot,
  syncGhostBaseKey,
  computeGhostBaseKey,
  type ChatContext,
} from './context';
import {
  linkCache,
  fetchDocumentLinksFresh,
  fetchReferenceLinksFresh,
  subscribeLinkCache,
  getLinkCacheVersion,
} from '../api/links';
import { useEvent } from '../hooks/useEvent';
import { t } from '../i18n';

/**
 * The ghost chat's context = deriveGhostContext(open entity + first-circle + deltas).
 * Reactive on app (currentDocument/currentReference) + ui (refOpenMode) + the ghost
 * delta store (manual picks + linkCacheVersion). Returns the SAME value
 * the send path snapshots at materialization (getDerivedGhostContext), so picker
 * display and the first turn agree.
 */
export function useGhostChatContext(): ChatContext {
  const docId = useAppStore(s => s.currentDocument?.document_id ?? null);
  const rawRefId = useAppStore(s => s.currentReference?.reference_id ?? null);
  // Subscribe to the open mode so a toggle re-derives. The open entity is ALWAYS in
  // the base (see the INVARIANT in ghost-context.ts) — no flag gates it. The ONE
  // exception-carrier is the MODE: in 'panel' quick preview the reference is a
  // viewer, not the scope, so it reads through refIsScope and the ghost base is
  // the document's (splitActive stays true — harmless with a null refId).
  const mode = useUIStore(s => readRefOpenMode(s.documents[docId ?? '']));
  const refId = refIsScope(mode) ? rawRefId : null;
  const splitActive = !!docId && showsBothPanes(mode);
  const snap = useGhostDeltaSnapshot();
  // Recompute on ANY linkCache mutation (set / delete / clear) — the version lives at
  // the cache's source (api/links.ts), so every writer drives recompute, not just
  // ghost-warm's fetch. See the INVARIANT on fetchAndCache (api/links.ts).
  const linkCacheVersion = useSyncExternalStore(subscribeLinkCache, getLinkCacheVersion);

  return useMemo(() => {
    const r = deriveGhostContext({ docId, refId, splitActive, linkCache, deltas: snap });
    return { documentIds: r.docIds, referenceIds: r.refIds };
  }, [docId, refId, splitActive, snap, linkCacheVersion]);
}

// In-flight first-circle fetches keyed by `kind:id`. Dedupes concurrent warm requests
// (the populated-cache guard alone cannot suppress a request that is still in flight,
// and an effect re-run between baseKey changes would otherwise fire each fetch ~2×).
const _pendingLinks = new Set<string>();

// Lost-update guard: a base key lands here when a fetch is requested (warm-effect
// re-run or editor-doc-changed event) while that key's fetch is ALREADY in flight —
// the in-flight response may predate the edit that caused the new request. The
// in-flight fetch's .finally re-enters the per-target fetch for any key it finds
// here (deleting the mark first), so at most ONE request per key is in flight at any
// time and the last request always wins. A bare parallel fetch would let two
// responses land out of order — the older one arriving last resurrects stale links.
const _rerunLinks = new Set<string>();

/** Test-only: clear the module-level in-flight/rerun sets — a test that ends with a
 * deferred fetch still pending would otherwise block the next test's warm. */
export function __resetLinkWarmState(): void {
  _pendingLinks.clear();
  _rerunLinks.clear();
}

interface WarmCycle {
  fetchFresh(kind: 'doc' | 'ref', id: string): void;
}

/**
 * One warm cycle = one shared failure escalation for a set of first-circle fetches.
 * Two callers: the mount/entity effect (isCancelled = its `cancelled` flag, one
 * cycle per effect run) and the editor-doc-changed handler (isCancelled = the hook's
 * unmount flag, one cycle per event). The factory stays module-private: the two
 * callers' needs (per-failure warn, one debounced toast per cycle, pending/rerun
 * discipline) are exactly this and nothing shared needs them.
 *
 * WHY toast + warn: a failed first-circle warm silently degrades the derived
 * selector to bare-id-only (the open entity stays, but its linked docs/refs
 * are missing) with NO signal — a formal no-silent-degradation violation. The
 * escalation is minimal: one console.warn per failed fetch (dev-loud) and a
 * SINGLE debounced toast per cycle (not per fetch — warm cycles are frequent, a
 * per-fetch toast would be noisy on transient blips). The open entity itself
 * still lands in context, so this is a partial, not total, degradation; the
 * toast just makes it explicit.
 */
function makeWarmCycle(isCancelled: () => boolean): WarmCycle {
  let failureScheduled = false;
  const scheduleFailureToast = () => {
    // One toast per cycle, regardless of how many fetches fail: the first failure
    // arms a single debounced timer; later failures see failureScheduled.
    if (failureScheduled) return;
    failureScheduled = true;
    setTimeout(() => {
      // Fire-time suppression (not timer-clearing): an unmounted caller leaves the
      // timer alive but mute. Cost if wrong: a failed refresh racing a panel close
      // goes unannounced — and the next open refetches fresh anyway.
      if (isCancelled()) return;
      useAppStore.getState().showToast(t('ghostContextLoadFailed'), 'error');
    }, 500);
  };

  const fetchTarget = (kind: 'doc' | 'ref', id: string): void => {
    const key = `${kind}:${id}`;
    _pendingLinks.add(key);
    const fetcher = kind === 'doc' ? fetchDocumentLinksFresh : fetchReferenceLinksFresh;
    // No bump here: the cache.set inside the fetcher bumps the shared version
    // (api/links.ts). This removes the `!cancelled`-guarded bump that the
    // cancelled-run race exploited. `isCancelled` only gates the failure toast.
    // The warn itself is NOT cancelled-gated: a post-unmount rejection is still a
    // dev-loud signal worth keeping — only the user-facing toast is suppressed.
    fetcher(id)
      .catch(() => {
        console.warn(
          `[ghost-context] warm first-circle fetch failed for ${key} — ` +
          'derived ghost context degrades to bare id (linked docs/refs unavailable).',
        );
        scheduleFailureToast();
      })
      .finally(() => {
        _pendingLinks.delete(key);
        // Rerun belongs to THIS cycle (its escalation too): the request that
        // marks _rerunLinks may be a newer event, but the fetch discipline is
        // per-key, not per-cycle. The rerun re-adds the pending mark below before
        // firing, exactly like any fetch — leaving it un-pending would let the
        // next event start a parallel request for the same key.
        if (_rerunLinks.delete(key)) fetchTarget(kind, id);
      });
  };

  return {
    // Stale-while-revalidate entry point: never invalidates before fetching (no
    // bare-id flash); the fresh response overwrites the cache entry in place.
    fetchFresh(kind, id) {
      const key = `${kind}:${id}`;
      if (_pendingLinks.has(key)) {
        _rerunLinks.add(key);
        return;
      }
      fetchTarget(kind, id);
    },
  };
}

/**
 * Warm the first-circle cache for the current ghost base (+ manual adds) so the
 * derived selector recomputes with full links. Mount with the chat panel. Uses the
 * *Fresh fetchers (fetch + cache-fill) — the codebase distrusts the warm cache for
 * derive decisions (links.ts:7-9).
 *
 * Contract: while the chat is a ghost (activeSessionId === null) its context follows
 * the open entity's CURRENT links. Base targets are ALWAYS fetched fresh on every
 * effect run (cache hit ignored), and live edits re-warm them again via the debounced
 * `editor-doc-changed` event (500 ms after the last keystroke, editor-observer.ts).
 * A materialized session keeps its stored snapshot (ghost-context.ts
 * INVARIANT(persisted)) — the event handler is gated on the ghost and its keys are
 * not refetched.
 */
export function useGhostContextWarm(): void {
  const docId = useAppStore(s => s.currentDocument?.document_id ?? null);
  const rawRefId = useAppStore(s => s.currentReference?.reference_id ?? null);
  const mode = useUIStore(s => readRefOpenMode(s.documents[docId ?? '']));
  // Same projection as the selector: warm exactly the ids the derived base folds —
  // a panel-previewed ref contributes nothing, so its first-circle is not fetched.
  const refId = refIsScope(mode) ? rawRefId : null;
  const splitActive = !!docId && showsBothPanes(mode);
  const snap = useGhostDeltaSnapshot();

  // The event handler reads the base through refs updated each render, so it never
  // refetches for the PREVIOUS entity. useEvent keeps the subscription stable
  // across renders (callbackRef), so the handler may be inline.
  const docIdRef = useRef(docId);
  docIdRef.current = docId;
  const refIdRef = useRef(refId);
  refIdRef.current = refId;
  const splitActiveRef = useRef(splitActive);
  splitActiveRef.current = splitActive;

  // Event-path cycles suppress their toast once the hook unmounts: a fetch still in
  // flight at unmount has no reader to toast for. Mirrors the effect's cancelled
  // flag (its cleanup sets it on every re-run).
  const unmountedRef = useRef(false);
  useEffect(() => {
    unmountedRef.current = false;
    return () => { unmountedRef.current = true; };
  }, []);

  useEvent('editor-doc-changed', () => {
    // WHY ghost-only gate: a materialized session keeps its STORED snapshot —
    // refetching first-circle for it is network cost without a reader.
    if (useChatStore.getState().activeSessionId !== null) return;
    // One cycle per event: failures across this event's base targets coalesce
    // into a single debounced toast.
    const cycle = makeWarmCycle(() => unmountedRef.current);
    const base = ghostBaseTargets(docIdRef.current, refIdRef.current, splitActiveRef.current);
    for (const id of base.docs) cycle.fetchFresh('doc', id);
    for (const id of base.refs) cycle.fetchFresh('ref', id);
  });

  useEffect(() => {
    syncGhostBaseKey(computeGhostBaseKey(docId, refId, splitActive));

    // Base targets use the SAME ghostBaseTargets the selector folds, so the warm
    // fetch always covers exactly the entities that appear in the derived base.
    const base = ghostBaseTargets(docId, refId, splitActive);
    const targets: Array<{ kind: 'doc' | 'ref'; id: string; isBase: boolean }> = [];
    for (const id of base.docs) targets.push({ kind: 'doc', id, isBase: true });
    for (const id of base.refs) targets.push({ kind: 'ref', id, isBase: true });
    for (const id of snap.addedDocIds) targets.push({ kind: 'doc', id, isBase: false });
    for (const id of snap.addedRefIds) targets.push({ kind: 'ref', id, isBase: false });

    let cancelled = false;
    // WHY: this loop looks like a duplicate of the createSession warm-before-snapshot
    //   block (sessions-slice.ts), but the two intentionally diverge and are NOT
    //   consolidated into a warmFirstCircle(targets) helper. This is the REACTIVE
    //   display path: it includes manual deltas, dedupes via _pendingLinks, relies on
    //   the cache.set inside the fetcher to bump the shared version so the selector
    //   recomputes, guards a cancelled flag, and escalates failure with a warn+toast
    //   (#4). createSession is a one-shot synchronous snapshot (base only, silent
    //   best-effort). A
    //   shared helper would force 3+ flags → param-flag branch; see the cross-link
    //   WHY in sessions-slice.ts.
    const cycle = makeWarmCycle(() => cancelled);

    for (const { kind, id, isBase } of targets) {
      const key = `${kind}:${id}`;
      if (isBase) {
        // WHY base-fresh (cache hit ignored): an EMPTIED document never invalidates
        // in-editor — checkpointContent's empty-read guard (INVARIANT(data-loss),
        // content-sync.ts) returns BEFORE syncToStore's invalidateLinkCache, so a
        // cached first-circle survives "select all + delete" and a reopen would read
        // the stale entry. Refetching every base on each effect run covers every
        // staleness source (own edit while the chat was closed, collaborator edit,
        // emptied doc) in one place.
        cycle.fetchFresh(kind, id);
        continue;
      }
      // Manual adds keep the skip-if-cached rule: they are user picks, not live
      // document state — no staleness source, and the cached circle is exactly what
      // the picker already shows.
      if (linkCache.has(key) || _pendingLinks.has(key)) continue;
      cycle.fetchFresh(kind, id);
    }
    return () => {
      cancelled = true;
    };
    // snap.addedDocIds / snap.addedRefIds are stable references except on a real
    // delta change, so this effect re-runs only on a real entity / delta change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId, refId, splitActive, snap.addedDocIds, snap.addedRefIds]);
}
