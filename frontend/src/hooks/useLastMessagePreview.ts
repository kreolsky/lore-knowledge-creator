/** Lazy-fetch the last-message preview for a chat session on hover. Module-level
 *  cache + in-flight dedup survive re-renders. Mirrors useDocumentPreview. */
// ARCH: folded onto the resource-cache primitive (see SYSTEM: swr-cache) — the LRU bound,
// the in-flight dedup and the generation-guarded clear live in createResourceCache.
// The cache is cleared on project switch (resetSiblingStores →
// clearLastMessagePreviewCache) and on soft logout via the logout registry (below).
// SYSTEM: chat-last-message-preview — lazy per-session last-message fetch for the
// chat-list plaque hover popup. Backend: GET /chat/sessions/{id}/last-message-preview.
// The session list stays content-free for AI chats (perf); this fetches ONE hovered
// session on demand — see the endpoint's WHY in backend/routes/chat/messages.py.

import { useState, useEffect } from 'react';
import { apiClient } from '../api/client';
import { createResourceCache } from '../api/swr-cache';
import { registerLogoutHandler } from '../store/logout-handlers';

const previewCache = createResourceCache<string>(50);

export function clearLastMessagePreviewCache() {
  previewCache.clear();
}

// Self-register the preview cache clear into the logout registry so
// setCurrentUser(null) drops it (see SYSTEM: logout-handlers — same shape as the
// sessions cache, sessions-slice/load-actions.ts). Why: a chat's last-message
// preview is user-scoped data; before this registration it survived a soft
// logout in the same tab and a same-tab re-login was served the prior user's
// preview as a free cache hit. clear() also refuses the settle write of any
// fetch still in flight across the logout (the primitive's generation guard).
registerLogoutHandler(clearLastMessagePreviewCache);

export function fetchLastMessagePreview(sessionId: string): Promise<string> {
  return previewCache.fetch(sessionId, () =>
    apiClient
      .get(`/chat/sessions/${sessionId}/last-message-preview`)
      .then((data: { preview?: string | null }) => (data.preview ?? '').trim()),
  );
}

export function useLastMessagePreview(sessionId: string | null): { content: string | undefined } {
  const [content, setContent] = useState<string | undefined>(() =>
    sessionId ? previewCache.get(sessionId) : undefined,
  );

  useEffect(() => {
    if (!sessionId) {
      setContent(undefined);
      return;
    }
    const cached = previewCache.get(sessionId);
    if (cached !== undefined) {
      setContent(cached);
      return;
    }
    let stale = false;
    fetchLastMessagePreview(sessionId)
      .then((text) => { if (!stale) setContent(text); })
      // No silent degradation: an empty string renders the popup's t('noContent')
      // fallback rather than stale content from a prior hovered row.
      .catch(() => { if (!stale) setContent(''); });
    return () => { stale = true; };
  }, [sessionId]);

  return { content };
}
