/**
 * The «Память» chip before the user message whose turn brought the chat its
 * conversation memory (chat_sessions.memory), drawn as a process chip
 * (ToolPlate). Collapsed: how many past chats the agent received; expanded:
 * their titles, fetched from GET /chat/sessions/{id}/memory (titles are
 * resolved at read, never stored), each opening that chat by its title and
 * previewing its last message on hover (as the chat list does). An
 * error record is a failed chip whose body says why the persona's memory is
 * off; an empty one a chip with a count of 0 whose body says memory is on with
 * nothing to bring yet.
 * Chat-store: setActiveSession only.
 */

import { useEffect, useState } from 'react';
import { Brain, MessageSquare } from 'lucide-react';
import { apiClient } from '../../api/client';
import { useChatStore } from '../../store/chat-store';
import { useTranslation } from '../../i18n';
import type { ChatMemoryRecord } from '../../types';
import { ToolPlate } from './ToolPlate';
import { HoverPreviewPopup } from '../HoverPreviewPopup';
import { useLastMessagePreview } from '../../hooks/useLastMessagePreview';
import { useRightPanelHoverPreview } from '../../hooks/useRightPanelHoverPreview';

interface ResolvedMemory {
  chats: { sid: string; title: string; date: string }[];
  hidden: number;
}

// WHY local: the i18n has no plural forms; Intl picks the CLDR category
// (en: one/other, ru: one/few/many/other) and each has its own key.
const ROW_KEY = {
  one: 'chatMemoryRow_one', few: 'chatMemoryRow_few', many: 'chatMemoryRow_many',
  two: 'chatMemoryRow_other', zero: 'chatMemoryRow_other', other: 'chatMemoryRow_other',
} as const;

type ListState = { status: 'loading' } | { status: 'error' } | { status: 'ready'; memory: ResolvedMemory };

/** The expanded body. Mounted on every expand (ToolPlate renders its body
 * only while open), so the titles are read fresh each time.
 * WHY no cache: a recorded chat can be renamed, deleted or hidden at any time. */
function MemoryChats({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation();
  const [list, setList] = useState<ListState>({ status: 'loading' });
  // The chat list's hover preview: the hovered chat's last message (SYSTEM: chat-last-message-preview).
  const hover = useRightPanelHoverPreview();
  const [hovered, setHovered] = useState<{ sid: string; title: string } | null>(null);
  const { content: hoverContent } = useLastMessagePreview(hovered?.sid ?? null);

  useEffect(() => {
    // WHY the flag: a collapse unmounts the body — a late response must not
    // set state on it.
    let live = true;
    apiClient.get(`/chat/sessions/${sessionId}/memory`)
      .then((res: { memory: ResolvedMemory | null }) => {
        if (live) setList(res.memory ? { status: 'ready', memory: res.memory } : { status: 'error' });
      })
      .catch(() => { if (live) setList({ status: 'error' }); });
    return () => { live = false; };
  }, [sessionId]);

  if (list.status === 'loading') return <div className="py-1 text-text-muted">{t('loading')}</div>;
  if (list.status === 'error') return <div className="py-1 text-text-muted" role="alert">{t('chatMemoryLoadFailed')}</div>;
  const open = (sid: string) => useChatStore.getState().setActiveSession(sid);
  return (
    <div className="flex flex-col">
      {list.memory.chats.map(c => (
        <div key={c.sid} className="flex items-center gap-1 min-w-0 py-1 text-text">
          <MessageSquare size={13} className="shrink-0" />
          {/* WHY only the title: the row's empty rest is not a target. */}
          <span
            role="link"
            tabIndex={0}
            className="truncate cursor-pointer hover:underline"
            onClick={() => open(c.sid)}
            onKeyDown={e => { if (e.key === 'Enter') open(c.sid); }}
            onMouseEnter={ev => { setHovered({ sid: c.sid, title: c.title || t('untitledLink') }); hover.handleHover(ev.currentTarget); }}
            onMouseLeave={hover.handleHoverLeave}
          >
            {c.title || t('untitledLink')}
          </span>
          <span className="shrink-0 text-text-muted">· {c.date.slice(0, 10)}</span>
        </div>
      ))}
      {list.memory.hidden > 0 && (
        <div className="py-1 text-text-muted">{t('chatMemoryHidden', { count: list.memory.hidden })}</div>
      )}
      <HoverPreviewPopup hover={hover} title={hovered?.title} content={hoverContent} />
    </div>
  );
}

export function ChatMemoryRow({ sessionId, record }: { sessionId: string; record: ChatMemoryRecord }) {
  const { t, language } = useTranslation();
  const rowTitle = (count: number) => t(ROW_KEY[new Intl.PluralRules(language).select(count)], { count });
  if (record.error) {
    return (
      <div data-chat-memory="error">
        <ToolPlate icon={<Brain size={13} />} title={<span>{t('chatMemoryOff')}</span>} tone="failed" bare>
          <div className="py-1 text-text-muted">{record.error}</div>
        </ToolPlate>
      </div>
    );
  }
  if (record.chats.length === 0) {
    return (
      <div data-chat-memory="empty">
        <ToolPlate icon={<Brain size={13} />} title={<span>{rowTitle(0)}</span>} bare>
          <div className="py-1 text-text-muted">{t('chatMemoryNoChats')}</div>
        </ToolPlate>
      </div>
    );
  }
  return (
    <div data-chat-memory="on">
      <ToolPlate
        icon={<Brain size={13} className="text-green" />}
        title={<span>{rowTitle(record.chats.length)}</span>}
        bare
      >
        <MemoryChats sessionId={sessionId} />
      </ToolPlate>
    </div>
  );
}
