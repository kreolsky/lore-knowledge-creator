/** Semantic search over the person's own chats — the request and the content-hit row, shared by the search tab and chat mode's chat list. */

import { apiClient, ForbiddenError } from '../../api/client';
import { formatDate } from '../../utils/format';
import { useTranslation } from '../../i18n';
import { ListPill } from '../ui';
import type { ChatSearchHit } from '../../types';

export const CHAT_SEARCH_MIN_CHARS = 2;

// The chat-search route's 403 while the administrator switched the chat-recall
// skill off — told apart from "No access to this project" by its detail.
const CHAT_RECALL_DISABLED_DETAIL = 'chat-recall is disabled by the administrator';
export const isChatRecallDisabled = (err: unknown) =>
  err instanceof ForbiddenError && err.detail === CHAT_RECALL_DISABLED_DETAIL;

/** Unscoped: the whole project by relevance, chats under the anchor marked. Scoped: only
 * chats attached to the anchor or its subtree. */
export async function fetchChatHits(
  projectId: string, query: string, anchorId: string | null | undefined, scoped: boolean,
): Promise<ChatSearchHit[]> {
  const params = new URLSearchParams({ q: query });
  params.set('scope', scoped && anchorId ? 'siblings' : 'project');
  if (anchorId) params.set('anchor_document_id', anchorId);
  const data: { hits: ChatSearchHit[] } = await apiClient.get(`/projects/${projectId}/chat-search?${params}`);
  return data.hits;
}

export function ChatSearchHitPill({ hit, onOpen }: { hit: ChatSearchHit; onOpen: (hit: ChatSearchHit) => void }) {
  const { t } = useTranslation();
  return (
    <ListPill
      variant="plain"
      data-chat-hit={hit.message_id}
      data-sibling={hit.sibling ? 'true' : undefined}
      onClick={() => onOpen(hit)}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-ui-base font-semibold text-text overflow-hidden text-ellipsis whitespace-nowrap">
          {hit.chat_title}
        </span>
        <span className="flex items-center gap-1.5 shrink-0">
          {hit.sibling && (
            <span className="text-ui-xs text-accent-text" title={t('chatSearchSiblingTip')}>
              {t('chatSearchSibling')}
            </span>
          )}
          {hit.created_at && (
            <span className="text-ui-xs text-text-dim">{formatDate(hit.created_at)}</span>
          )}
          <span className="text-ui-xs text-text-dim bg-surface3 py-px px-1.5">
            {(hit.score * 100).toFixed(0)}%
          </span>
        </span>
      </div>
      {hit.question && (
        <div className="text-ui-xs text-text-dim mt-0.5 italic line-clamp-1">{hit.question}</div>
      )}
      <div className="text-xs text-text-dim mt-1.5 leading-relaxed line-clamp-3">
        {hit.snippet}
      </div>
    </ListPill>
  );
}
