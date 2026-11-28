/** Chat mode's left panel: a search field over the project's chat list, newest first. Reuses ChatEmptyList (rows exactly as in the chat tab).
 * Typing filters the rows by title at once; from CHAT_SEARCH_MIN_CHARS the semantic chat search (shared with the search tab) adds content hits below them. Esc in the field clears it.
 * Store slices: app-store currentProject, currentDocument; chat-store openBranch, setPendingScroll. */

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useAppStore } from '../../store/app-store';
import { useChatStore } from '../../store/chat-store';
import { useTranslation } from '../../i18n';
import { Button, FieldInput } from '../ui';
import { ChatEmptyList } from './ChatEmptyList';
import { CHAT_SEARCH_MIN_CHARS, ChatSearchHitPill, fetchChatHits, isChatRecallDisabled } from './chat-search';
import { filterSessionsByTitle, useSortedSessions } from './useSortedSessions';
import type { ChatSearchHit } from '../../types';

type ContentState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'failed'; disabled: boolean }
  | { status: 'done'; hits: ChatSearchHit[] };

const DEBOUNCE_MS = 300;

export function ChatModeList() {
  const { t } = useTranslation();
  const projectId = useAppStore(s => s.currentProject?.project_id);
  const sorted = useSortedSessions();
  const [query, setQuery] = useState('');
  const [content, setContent] = useState<ContentState>({ status: 'idle' });
  const [retryTick, setRetryTick] = useState(0);
  const requestRef = useRef(0);

  const trimmed = query.trim();
  useEffect(() => {
    const request = ++requestRef.current;
    if (!projectId || trimmed.length < CHAT_SEARCH_MIN_CHARS) { setContent({ status: 'idle' }); return; }
    setContent({ status: 'loading' });
    const timer = setTimeout(() => {
      // Unscoped: chat mode searches the whole project; the open document only marks its chats.
      fetchChatHits(projectId, trimmed, useAppStore.getState().currentDocument?.document_id, false)
        .then(hits => { if (request === requestRef.current) setContent({ status: 'done', hits }); })
        .catch((err: unknown) => {
          if (request === requestRef.current) setContent({ status: 'failed', disabled: isChatRecallDisabled(err) });
        });
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [projectId, trimmed, retryTick]);

  const titleIds = useMemo(
    () => new Set(filterSessionsByTitle(sorted, trimmed).map(s => s.session_id)),
    [sorted, trimmed],
  );
  // WHY drop the title-matched chats: their row is already listed above, one row per chat.
  const contentHits = content.status === 'done' ? content.hits.filter(h => !titleIds.has(h.chat_id)) : [];

  const openHit = (hit: ChatSearchHit) => {
    const chat = useChatStore.getState();
    chat.openBranch(hit.chat_id, hit.thread_id);
    chat.setPendingScroll(hit.chat_id, hit.message_id, hit.snippet);
  };

  const status = (text: string, action?: ReactNode) => (
    <p className="text-ui-base text-text-dim text-center py-4 px-2">{text}{action && <>{' '}{action}</>}</p>
  );
  const nothingFound = trimmed !== '' && titleIds.size === 0 && contentHits.length === 0
    && (content.status === 'idle' || content.status === 'done');
  const footer = trimmed === '' ? null : (
    <>
      {contentHits.map(h => <ChatSearchHitPill key={`${h.chat_id}-${h.message_id}`} hit={h} onOpen={openHit} />)}
      {content.status === 'loading' && status(t('searching'))}
      {content.status === 'failed' && content.disabled && status(t('chatSearchDisabled'))}
      {content.status === 'failed' && !content.disabled && status(t('searchFailed'),
        <Button type="button" variant="ghost" size="sm" onClick={() => setRetryTick(n => n + 1)}>{t('retry')}</Button>)}
      {nothingFound && status(t('noMatchesFound'))}
    </>
  );

  return (
    <div data-testid="chat-mode-list" className="flex-1 flex flex-col min-h-0">
      {/* WHY pt-2 px-2, no bottom: the field's edges line up with the pills (.pill-list's 8px inset); the list's own 8px top padding is the gap below. */}
      {/* WHY the fade: rows scrolled up dissolve into the panel's --surface below the field instead of being cut at its edge. */}
      <div className="relative z-[1] pt-2 pb-1 px-2 bg-surface">
        <div aria-hidden className="pointer-events-none absolute inset-x-0 top-full h-3 bg-linear-to-b from-surface to-transparent" />
        <FieldInput
          placeholder={t('searchPlaceholderChats')}
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => { if (e.key === 'Escape' && query) { e.preventDefault(); e.stopPropagation(); setQuery(''); } }}
        />
      </div>
      <ChatEmptyList activeColor="tree" titleQuery={trimmed} footer={footer} />
    </div>
  );
}
