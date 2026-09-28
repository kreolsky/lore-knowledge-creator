/** Lazy-fetch document content for hover previews. Module-level cache survives re-renders. */
// ARCH: folded onto the resource-cache primitive (see SYSTEM: swr-cache) — the LRU bound,
// the in-flight dedup and the generation-guarded clear live in createResourceCache;
// this module keeps only the loader, the seed/invalidate entry points and the hook.
// The cache is cleared on project switch by resetEditorHost's project scope
// (components/editor/editor-host.ts) and on soft logout via the logout registry
// (below) — two distinct lifecycles, both caller-visible through clearPreviewCache.
// SYSTEM: document-preview — lazy document content fetch with cross-component cache

import { useState, useEffect } from 'react';
import { apiClient } from '../api/client';
import { getPublicFileContext } from '../utils/reference-url';
import { createResourceCache } from '../api/swr-cache';
import { registerLogoutHandler } from '../store/logout-handlers';

const previewCache = createResourceCache<string>(50);

export function clearPreviewCache() {
  previewCache.clear();
}

// Self-register the preview cache clear into the logout registry so
// setCurrentUser(null) drops it (see SYSTEM: logout-handlers — same shape as the
// sessions cache, sessions-slice/load-actions.ts). Why: a document body is
// user-scoped data; before this registration it survived a soft logout in the
// same tab and a same-tab re-login was served the prior user's body as a free
// cache hit. clear() also refuses the settle write of any fetch still in
// flight across the logout (the primitive's generation guard).
registerLogoutHandler(clearPreviewCache);

export function invalidatePreviewCache(docId: string) {
  previewCache.invalidate(docId);
}

/** Seed the cache from a batch result (the transclusion batch) so a later
 *  hover-preview of a batched id is a free cache hit. Mirrors the cache-write path
 *  inside fetchDocumentContent. */
export function seedDocPreview(docId: string, content: string): void {
  previewCache.seed(docId, content);
}

/**
 * see SYSTEM: transclusion — fetches a document's content through the same cache as
 * useDocumentPreview. Resolves to the content string ('' for a legitimately-empty doc);
 * REJECTS on a genuine fetch failure (network/403/etc.) so callers can surface the error
 * instead of silently keeping stale content. Failed fetches are NOT cached, so a retry
 * re-fetches. Concurrent same-id callers collapse to one in-flight GET. Used to seed
 * sibling-document transclusion entries.
 *
 * PUBLIC-SHARE path: when `setPublicFileContext(rootDocId)` is active
 * (PublicSharePage mount), routes through the document-keyed
 * `/public/documents/{id}` instead of the authed `/documents/{id}`. WHY: the
 * authed endpoint 401s anonymous callers and the apiClient redirect-on-401
 * guard kicks the visitor off /docs/:id. The anonymous surface returns the same
 * `{ content }` shape, so no special parsing is needed. An out-of-scope doc
 * (sibling moved out of the share subtree) 404s on the public path — the
 * rejection surfaces as the preview's error state (no silent empty).
 */
export function fetchDocumentContent(docId: string): Promise<string> {
  return previewCache.fetch(docId, () => {
    // WHY read the public context at call time (not at module load): it is
    // set/cleared on PublicSharePage mount/unmount and may change within a tab
    // session (authed → public → authed). A module-load capture would freeze the
    // wrong surface. Mirrors referenceFileUrl / referenceThumbUrl. The context is
    // now the share-root document id (plan "public-document-ids"); it gates the
    // public branch — the doc's OWN id is the path segment (document-keyed).
    const publicRootDocId = getPublicFileContext();
    const url = publicRootDocId
      ? `/public/documents/${docId}`
      : `/documents/${docId}`;
    return apiClient.get(url).then((data: { content?: string }) => data.content ?? '');
  });
}

export function useDocumentPreview(docId: string | null): {
  content: string | undefined;
  error: boolean;
  loading: boolean;
} {
  // INVARIANT: both state fields carry the id they describe, and a result is read only when
  // its id still matches the requested docId. Why: state outlives a docId change by one
  // render (the effect runs after paint), so an id-less `content` painted document A's body
  // under a hover of document B for a frame — stale content shown as current, and `loading`
  // computed false while B had not been fetched at all.
  const [entry, setEntry] = useState<{ id: string; content: string } | undefined>(() => {
    const cached = docId ? previewCache.get(docId) : undefined;
    return docId && cached !== undefined ? { id: docId, content: cached } : undefined;
  });
  const [errorId, setErrorId] = useState<string | null>(null);

  const content = entry?.id === docId ? entry.content : undefined;
  const error = docId != null && errorId === docId;

  useEffect(() => {
    if (!docId) return;

    const cached = previewCache.get(docId);
    if (cached !== undefined) {
      setEntry({ id: docId, content: cached });
      return;
    }

    let stale = false;
    // Routes through fetchDocumentContent — one fetch path, one cache-write site, and the
    // shared in-flight dedup (a hover racing a transclusion collapses to one GET).
    fetchDocumentContent(docId).then(
      (text) => { if (!stale) setEntry({ id: docId, content: text }); },
      // WHY(no-silent-degradation): a failed fetch records `errorId` and writes NOTHING
      // to the cache — it never stores ''. Why: caching '' made the failure sticky for the
      // whole tab session (the cache hit above served the poison without re-fetching), and ''
      // renders as "no content" — a doc that exists and failed to load looked empty.
      () => { if (!stale) setErrorId(docId); },
    );

    return () => { stale = true; };
  }, [docId]);

  // Derived, never inferred by the consumer: a cache hit fills `entry` in the useState
  // initializer, so `loading` is already false on the first paint (no spinner flash).
  return { content, error, loading: docId != null && content === undefined && !error };
}
