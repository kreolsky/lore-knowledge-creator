/**
 * The panel /references fetch on the resource-cache primitive.
 *
 * ARCH: the in-flight collapse and the SWR list cache are the primitive's
 * (see SYSTEM: swr-cache) — `revalidate` gives the always-revalidate contract:
 * concurrent same-scope GETs share ONE round-trip (an in-app
 * `navigate-to-document` commits the document TWICE — Header's optimistic
 * `setCurrentDocument`, then DocumentPage's prefetch commit — so
 * ReferencesPanel's scope effect fires its GET twice in the same tick), the
 * load ALWAYS re-runs on a later open, and the settle write is refused when a
 * clear() (logout via the registry at app-store, test reset) ran mid-flight.
 * The panel-load count==1 browser-drive assertion relies on that dedup.
 *
 * INVARIANT: this is NOT a results cache for correctness — every open
 * refetches; the cached list is a PRE-gate paint via `onCached`. Why:
 * references mutate via WS/upload, so a results cache would go stale on a
 * missed event; always-revalidate is the correctness backstop and the cache
 * only paints instantly while the fresh result commits.
 *
 * ARCH: index_doc_id is intentionally NOT part of the scope key (or the request).
 * The server resolves the project's index doc itself and always includes it in the
 * ancestor scope (references.py::list_references), so the param became redundant.
 * Dropping it client-side collapses DocumentPage's prefetch and the panel/restore
 * fetch — which previously had different scope keys (with vs without index_doc_id)
 * and thus fetched twice — into ONE round-trip. The server-side resolve also fixes
 * the orphan/broken-chain case the old client param existed to paper over.
 */
import type { Reference } from '../types';
import { apiClient } from './client';
import { createResourceCache } from './swr-cache';

// ARCH: SWR layer — a stale list paints instantly on re-open while the
// revalidate fetch below always runs and commits the fresh result. The cache
// mirrors store state via the per-item WS handlers; the always-revalidate
// fetch is the correctness backstop.
const refsCache = createResourceCache<Reference[]>();

export function loadReferences(
  projectId: string,
  documentId: string,
  onCached?: (refs: Reference[]) => void,
  includeArchived?: boolean,
): Promise<Reference[]> {
  // ARCH: `includeArchived` is folded into the scopeKey (not
  // just the URL). Why: the SWR cache (refsCache) + the in-flight dedup map are keyed by
  // scopeKey — a toggle flip must NOT be served the stale non-archived list (cache hit)
  // nor swallowed by an in-flight non-archived request (dedup). Including the flag makes
  // "archived" a distinct scope with its own cache entry + its own in-flight slot, so the
  // toggle actually changes the server query.
  const scopeKey = `${projectId}:${documentId}:${includeArchived ? '1' : '0'}`;

  // PRE-gate paint: hand back the last-seen list synchronously for an instant
  // render. NOT a terminal — the revalidate fetch below still runs and commits.
  if (onCached) {
    const cached = refsCache.get(scopeKey);
    if (cached) onCached(cached);
  }

  const query = includeArchived
    ? `/references?document_id=${documentId}&include_archived=true`
    : `/references?document_id=${documentId}`;
  return refsCache.revalidate(scopeKey, () => apiClient.get(query));
}

/** Test-only: reset the SWR cache between cases (also the logout-registry clear). */
export function clearReferencesInFlight(): void {
  refsCache.clear();
}
