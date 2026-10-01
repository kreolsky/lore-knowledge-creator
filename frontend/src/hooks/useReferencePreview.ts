/** Lazy-fetch reference content/image for hover previews + transclusions. Module-level cache survives re-renders. */
// ARCH: the shared content source for reference bodies now that the LIST endpoint is
// metadata-only. Consumed by the hover-preview hook AND the transclusion lazy-fetch
// effect (useEditorReferenceSync), so a single GET backs both. The in-store list ref
// carries no content; callers that need the body route through fetchRefPreview (cache)
// or hydrateReference (full ref incl. headings, for the editor). The cache is LRU via
// the resource-cache primitive (see SYSTEM: swr-cache), cleared on project switch by
// resetEditorHost's project scope (see SYSTEM: editor-host — it is transclusion content,
// so it is cleared alongside the document body cache) and on soft logout via the logout
// registry (below), and invalidated per-ref on WS reference_update/delete
// (invalidateRefPreview) so a stale body is never served — the primitive refuses the
// settle write of a fetch the invalidate overtook.
// SYSTEM: reference-preview — lazy reference content fetch with cross-component cache

import { useState, useEffect } from 'react';
import { apiClient } from '../api/client';
import { Reference } from '../types';
import { referenceFileUrl } from '../utils/reference-url';
import { createResourceCache } from '../api/swr-cache';
import { registerLogoutHandler } from '../store/logout-handlers';

export interface RefPreview {
  /** The reference's title — the preview plaque for refs absent from the store. */
  title?: string;
  content?: string;
  imageUrl?: string;
}

const previewCache = createResourceCache<RefPreview>(50);

export function clearRefPreviewCache() {
  previewCache.clear();
}

// Self-register the preview cache clear into the logout registry so
// setCurrentUser(null) drops it (see SYSTEM: logout-handlers — same shape as the
// sessions cache, sessions-slice/load-actions.ts). Why: a reference body is
// user-scoped data; before this registration it survived a soft logout in the
// same tab and a same-tab re-login was served the prior user's body as a free
// cache hit. clear() also refuses the settle write of any fetch still in
// flight across the logout (the primitive's generation guard).
registerLogoutHandler(clearRefPreviewCache);

/** Evict a single cache entry. Called on WS reference_update / reference_delete so a
 *  stale body is never served (no-silent-degradation): the preview cache is now the
 *  content source for BOTH hover previews and transclusions. */
export function invalidateRefPreview(refId: string): void {
  previewCache.invalidate(refId);
}

/** Seed the cache from a batch result (the transclusion batch) so a later
 *  hover-preview of a batched id is a free cache hit instead of a re-fetch. Mirrors
 *  the cache-write path inside fetchRefPreview. */
export function seedRefPreview(refId: string, preview: RefPreview): void {
  previewCache.seed(refId, preview);
}

/** Cache-backed single-ref fetch — the shared content source for hover previews AND
 *  transclusions. Returns the cached preview on hit; otherwise GETs /references/{id},
 *  caches, and returns. Resolves to `{ content: '' }` for a legitimately-empty ref;
 *  REJECTS on a genuine fetch failure (network/404/etc.) so callers can surface the error
 *  instead of rendering it as an empty body. Failed fetches are NOT cached, so a retry
 *  re-fetches. Concurrent same-id callers collapse to one in-flight GET. The cache is
 *  LRU-evicted, cleared on project switch + soft logout, and invalidated on WS
 *  mutations (see clearRefPreviewCache / invalidateRefPreview). Mirrors
 *  fetchDocumentContent. */
export function fetchRefPreview(refId: string): Promise<RefPreview> {
  // INVARIANT(no-silent-degradation): a failed fetch REJECTS and is NOT cached — it is
  // never resolved as an empty body. Why: `{ content: '' }` is indistinguishable from a
  // genuinely-empty ref at the render site, so a transient error would read as "no
  // content"; caching it would poison every later hover/transclusion for the id until a
  // WS invalidate. Rejecting lets the caller show an error, and leaves the next call free
  // to retry. Empty state ≠ error state (mirrors PanelLoading's "Empty state ≠ loading
  // state").
  return previewCache.fetch(refId, () =>
    apiClient.get(`/references/${refId}`).then((ref) => buildRefPreview(ref as Reference)),
  );
}

export function buildRefPreview(ref: Reference): RefPreview {
  if (ref.media_type === 'image' && ref.file_path)
    return { title: ref.title, imageUrl: referenceFileUrl(ref.reference_id, ref.file_path) };
  return { title: ref.title, content: ref.content ?? '' };
}

export function useReferencePreview(refId: string | null): {
  preview: RefPreview | undefined;
  error: boolean;
  loading: boolean;
} {
  // INVARIANT: both state fields carry the id they describe, and a result is read only when
  // its id still matches the requested refId. Why: state outlives a refId change by one
  // render (the effect runs after paint), so an id-less preview painted reference A's body
  // under a hover of reference B for a frame. Mirrors useDocumentPreview.
  const [entry, setEntry] = useState<{ id: string; preview: RefPreview } | undefined>(() => {
    const cached = refId ? previewCache.get(refId) : undefined;
    return refId && cached !== undefined ? { id: refId, preview: cached } : undefined;
  });
  const [errorId, setErrorId] = useState<string | null>(null);

  const preview = entry?.id === refId ? entry.preview : undefined;
  const error = refId != null && errorId === refId;

  useEffect(() => {
    if (!refId) return;

    const cached = previewCache.get(refId);
    if (cached !== undefined) {
      setEntry({ id: refId, preview: cached });
      return;
    }

    let stale = false;
    fetchRefPreview(refId).then(
      (p) => { if (!stale) setEntry({ id: refId, preview: p }); },
      // INVARIANT(no-silent-degradation): a failed fetch records `errorId` — it never sets an
      // empty preview. Why: an empty body renders as "no content", which is a lie about a
      // ref that exists and failed to load.
      () => { if (!stale) setErrorId(refId); },
    );

    return () => { stale = true; };
  }, [refId]);

  // Derived, never inferred by the consumer: a cache hit fills `entry` in the useState
  // initializer, so `loading` is already false on the first paint (no spinner flash).
  return { preview, error, loading: refId != null && preview === undefined && !error };
}
