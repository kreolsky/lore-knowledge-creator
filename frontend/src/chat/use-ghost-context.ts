/**
 * Reactive bindings for the DERIVED ghost context.
 *
 * context.ts stays a pure store module (no app/ui import, no React render hooks) —
 * these hooks wire it to the app/ui stores. The ghost (zero-chat) auto-context is a
 * pure function of the open entity + first-circle + manual deltas; it is recomputed
 * on every read and NEVER stored.
 */

import { useEffect, useMemo, useSyncExternalStore } from 'react';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { readRefOpenMode, showsBothPanes, refIsScope } from '../store/ui-store/documents-slice';
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

/**
 * Warm the first-circle cache for the current ghost base (+ manual adds) so the
 * derived selector recomputes with full links. Mount with the chat panel. Uses the
 * *Fresh fetchers (fetch + cache-fill) — the codebase distrusts the warm cache for
 * derive decisions (links.ts:7-9). RESIDUAL staleness: links edited via body content
 * while the SAME entity stays open (no baseKey change → no re-fetch) — content-sync
 * only invalidates on STRUCTURAL link add/remove; narrow, matches prior behavior.
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

  useEffect(() => {
    syncGhostBaseKey(computeGhostBaseKey(docId, refId, splitActive));

    // Base targets use the SAME ghostBaseTargets the selector folds, so the warm
    // fetch always covers exactly the entities that appear in the derived base.
    const base = ghostBaseTargets(docId, refId, splitActive);
    const targets: Array<{ kind: 'doc' | 'ref'; id: string }> = [];
    for (const id of base.docs) targets.push({ kind: 'doc', id });
    for (const id of base.refs) targets.push({ kind: 'ref', id });
    for (const id of snap.addedDocIds) targets.push({ kind: 'doc', id });
    for (const id of snap.addedRefIds) targets.push({ kind: 'ref', id });

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
    // WHY toast + warn: a failed first-circle warm silently degrades the derived
    // selector to bare-id-only (the open entity stays, but its linked docs/refs
    // are missing) with NO signal — a formal no-silent-degradation violation. The
    // escalation is minimal: one console.warn per failed fetch (dev-loud) and a
    // SINGLE debounced toast per warm cycle (not per fetch — warm cycles are
    // frequent, a per-fetch toast would be noisy on transient blips). The open
    // entity itself still lands in context, so this is a partial, not total,
    // degradation; the toast just makes it explicit.
    let failureScheduled = false;
    let failureTimer: ReturnType<typeof setTimeout> | null = null;
    const scheduleFailureToast = () => {
      // One toast per warm cycle, regardless of how many fetches fail: the first
      // failure arms a single debounced timer; later failures see failureScheduled.
      if (failureScheduled) return;
      failureScheduled = true;
      failureTimer = setTimeout(() => {
        failureTimer = null;
        if (cancelled) return;
        useAppStore.getState().showToast(t('ghostContextLoadFailed'), 'error');
      }, 500);
    };

    for (const { kind, id } of targets) {
      const key = `${kind}:${id}`;
      if (linkCache.has(key) || _pendingLinks.has(key)) continue;
      _pendingLinks.add(key);
      const fetcher = kind === 'doc' ? fetchDocumentLinksFresh : fetchReferenceLinksFresh;
      // No bump here: the cache.set inside the fetcher bumps the shared version
      // (api/links.ts). This removes the `!cancelled`-guarded bump that the
      // cancelled-run race exploited. `cancelled` now only gates the failure toast.
      fetcher(id)
        .catch(() => {
          if (cancelled) return;
          console.warn(
            `[ghost-context] warm first-circle fetch failed for ${key} — ` +
            'derived ghost context degrades to bare id (linked docs/refs unavailable).',
          );
          scheduleFailureToast();
        })
        .finally(() => { _pendingLinks.delete(key); });
    }
    return () => {
      cancelled = true;
      if (failureTimer) clearTimeout(failureTimer);
    };
    // snap.addedDocIds / snap.addedRefIds are stable references except on a real
    // delta change, so this effect re-runs only on a real entity / delta change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId, refId, splitActive, snap.addedDocIds, snap.addedRefIds]);
}
