/** First-circle link fetchers for chat content-picker cascade. */

import { apiClient } from './client';
import type { FirstCircle } from '../utils/cascade-selection';

const cache = new Map<string, FirstCircle>();

// INVARIANT: every linkCache mutation (set / delete / clear) bumps `version` and
// notifies subscribers; the derived ghost selector (useGhostChatContext) subscribes
// via subscribeLinkCache. Why: the version is what makes a plain-Map mutation reactive
// — before this, only ONE writer (ghost-warm's own .then, `!cancelled`-guarded) bumped,
// so a cancelled-run / pending-dedup race (StrictMode double-mount; base change with an
// in-flight warm) filled the cache without bumping and the memo stayed on cold data
// (first circle missing in the picker until a manual toggle). Versioning at the source
// makes EVERY cache writer drive recompute.
let version = 0;
const listeners = new Set<() => void>();

function bumpVersion(): void {
  version += 1;
  for (const cb of listeners) cb();
}

// ARCH: `fresh` skips the cache read so the chat cascade never decides which entities
// to attach from a stale first-circle (the cache is never durably invalidated on every
// keystroke). The warm cache only feeds the non-critical Link2 picker indicator.
async function fetchAndCache(key: string, url: string, fresh = false): Promise<FirstCircle> {
  if (!fresh) {
    const hit = cache.get(key);
    if (hit) return hit;
  }
  const data = await apiClient.get(url) as FirstCircle;
  const normalized: FirstCircle = {
    document_ids: data.document_ids ?? [],
    reference_ids: data.reference_ids ?? [],
  };
  cache.set(key, normalized);
  bumpVersion();
  return normalized;
}

export const fetchDocumentLinks = (id: string) =>
  fetchAndCache(`doc:${id}`, `/documents/${id}/links`);

export const fetchReferenceLinks = (id: string) =>
  fetchAndCache(`ref:${id}`, `/references/${id}/links`);

/** Force-fresh variants for the chat cascade — always re-fetch, still refresh the cache. */
export const fetchDocumentLinksFresh = (id: string) =>
  fetchAndCache(`doc:${id}`, `/documents/${id}/links`, true);

export const fetchReferenceLinksFresh = (id: string) =>
  fetchAndCache(`ref:${id}`, `/references/${id}/links`, true);

/** Drop one cached first-circle so the Link2 indicator stops showing a stale hint. */
export const invalidateLinkCache = (kind: 'doc' | 'ref', id: string): void => {
  if (cache.delete(`${kind}:${id}`)) bumpVersion();
};

export const clearLinkCache = (): void => {
  if (cache.size === 0) return;
  cache.clear();
  bumpVersion();
};

/** Read-only view of the cache shared with the picker for the Link2 indicator. */
export const linkCache: ReadonlyMap<string, FirstCircle> = cache;

// SYSTEM cross-ref: the derived ghost selector (see SYSTEM: ghost-context) subscribes
// here so it recomputes on any cache mutation. useSyncExternalStore-shaped pair.
/** Subscribe to linkCache mutations. Returns an unsubscribe fn. */
export const subscribeLinkCache = (cb: () => void): (() => void) => {
  listeners.add(cb);
  return () => { listeners.delete(cb); };
};

/** Current linkCache version — increments on every set / delete / clear. */
export const getLinkCacheVersion = (): number => version;
