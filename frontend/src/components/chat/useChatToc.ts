/** Fetches a chat's table of contents on demand — local state, one consumer (PromptRailTopics). */

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiClient } from '../../api/client';

export interface ChatTocTopic {
  title: string;
  synopsis: string;
  message_ids: string[];
}

export interface ChatToc {
  topics: ChatTocTopic[];
  labelled_ids: string[];
}

/** Why the chat has no toc (GET …/toc `reason`, sent only with `toc: null`). */
export type ChatTocReason = 'recall_off' | 'unindexed' | 'memory_off' | 'pending';

export type ChatTocState =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'ready'; toc: ChatToc }
  | { status: 'none'; reason: ChatTocReason };

/**
 * The ToC of `sessionId`, fetched on every mount and on `reload`.
 * WHY no cache: the labeller rewrites the row in the background, so a cached copy
 * would be served as current while stale. A response for a superseded request
 * (session switch, unmount, a newer reload) is aborted and dropped.
 */
export function useChatToc(sessionId: string): { state: ChatTocState; reload: () => void } {
  const [state, setState] = useState<ChatTocState>({ status: 'loading' });
  const abortRef = useRef<AbortController | null>(null);

  const reload = useCallback(() => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setState({ status: 'loading' });
    apiClient.get(`/chat/sessions/${sessionId}/toc`, { signal: ctrl.signal })
      .then((res: { toc: ChatToc | null; reason?: ChatTocReason }) => {
        if (ctrl.signal.aborted) return;
        setState(res.toc ? { status: 'ready', toc: res.toc } : { status: 'none', reason: res.reason ?? 'pending' });
      })
      .catch(() => {
        if (!ctrl.signal.aborted) setState({ status: 'error' });
      });
  }, [sessionId]);

  useEffect(() => {
    reload();
    return () => abortRef.current?.abort();
  }, [reload]);

  return { state, reload };
}
