/**
 * The «Память» and «Вспомнил» chips before the user message whose turn brought
 * the chat its conversation memory (chat_sessions.memory) or its auto-recall
 * (chat_sessions.recall), each drawn as a process chip (ToolPlate). Collapsed:
 * how many past chats the agent received; expanded: their titles, fetched from
 * GET /chat/sessions/{id}/memory (titles are resolved at read, never stored),
 * each opening that chat by its title and previewing its last message on
 * hover (as the chat list does).
 * A memory error record is a failed chip whose body says why the persona's
 * memory is off; an empty one a chip with a count of 0 whose body says memory
 * is on with nothing to bring yet. A recall record is written only with hits,
 * so the recall chip has the "on" branch alone — the two rows share it.
 * Chat-store: setActiveSession only.
 */

import { useEffect, useState } from 'react';
import { Brain, History, MessageSquare } from 'lucide-react';
import { apiClient } from '../../api/client';
import { useChatStore } from '../../store/chat-store';
import { useTranslation } from '../../i18n';
import type { ChatMemoryRecord, ChatRecallRecord } from '../../types';
import { ToolPlate } from './ToolPlate';
import { HoverPreviewPopup } from '../HoverPreviewPopup';
import { useLastMessagePreview } from '../../hooks/useLastMessagePreview';
import { useRightPanelHoverPreview } from '../../hooks/useRightPanelHoverPreview';

type Kind = 'memory' | 'recall';

interface ResolvedChats {
  chats: { sid: string; title: string; date: string }[];
  hidden: number;
}

// WHY local: the i18n has no plural forms; Intl picks the CLDR category
// (en: one/other, ru: one/few/many/other) and each has its own key.
const ROW_KEY = {
  memory: {
    one: 'chatMemoryRow_one', few: 'chatMemoryRow_few', many: 'chatMemoryRow_many',
    two: 'chatMemoryRow_other', zero: 'chatMemoryRow_other', other: 'chatMemoryRow_other',
  },
  recall: {
    one: 'chatRecallRow_one', few: 'chatRecallRow_few', many: 'chatRecallRow_many',
    two: 'chatRecallRow_other', zero: 'chatRecallRow_other', other: 'chatRecallRow_other',
  },
} as const;

const ROW_ICON = { memory: Brain, recall: History };

type ListState = { status: 'loading' } | { status: 'error' } | { status: 'ready'; chats: ResolvedChats };

function useRowTitle(kind: Kind): (count: number) => string {
  const { t, language } = useTranslation();
  return count => t(ROW_KEY[kind][new Intl.PluralRules(language).select(count)], { count });
}

/** The expanded body. Mounted on every expand (ToolPlate renders its body
 * only while open), so the titles are read fresh each time.
 * WHY no cache: a recorded chat can be renamed, deleted or hidden at any time. */
function RecordChats({ sessionId, kind }: { sessionId: string; kind: Kind }) {
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
      .then((res: Record<Kind, ResolvedChats | null>) => {
        const chats = res[kind];
        if (live) setList(chats ? { status: 'ready', chats } : { status: 'error' });
      })
      .catch(() => { if (live) setList({ status: 'error' }); });
    return () => { live = false; };
  }, [sessionId, kind]);

  if (list.status === 'loading') return <div className="py-1 text-text-muted">{t('loading')}</div>;
  if (list.status === 'error') {
    return <div className="py-1 text-text-muted" role="alert">{t(kind === 'memory' ? 'chatMemoryLoadFailed' : 'chatRecallLoadFailed')}</div>;
  }
  const open = (sid: string) => useChatStore.getState().setActiveSession(sid);
  return (
    <div className="flex flex-col">
      {list.chats.chats.map(c => (
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
      {list.chats.hidden > 0 && (
        <div className="py-1 text-text-muted">{t('chatMemoryHidden', { count: list.chats.hidden })}</div>
      )}
      <HoverPreviewPopup hover={hover} title={hovered?.title} content={hoverContent} />
    </div>
  );
}

/** The chip of a record with chats — the one branch both kinds share. */
function RecordChip({ sessionId, kind, record }: { sessionId: string; kind: Kind; record: ChatMemoryRecord | ChatRecallRecord }) {
  const rowTitle = useRowTitle(kind);
  const Icon = ROW_ICON[kind];
  return (
    <div {...{ [`data-chat-${kind}`]: 'on' }}>
      <ToolPlate icon={<Icon size={13} className="text-green" />} title={<span>{rowTitle(record.chats.length)}</span>} bare>
        <RecordChats sessionId={sessionId} kind={kind} />
      </ToolPlate>
    </div>
  );
}

export function ChatMemoryRow({ sessionId, record }: { sessionId: string; record: ChatMemoryRecord }) {
  const { t } = useTranslation();
  const rowTitle = useRowTitle('memory');
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
  return <RecordChip sessionId={sessionId} kind="memory" record={record} />;
}

/** The auto-recall chip: the record is written only with hits, so it has no
 * error and no empty branch. */
export function ChatRecallRow({ sessionId, record }: { sessionId: string; record: ChatRecallRecord }) {
  return <RecordChip sessionId={sessionId} kind="recall" record={record} />;
}
