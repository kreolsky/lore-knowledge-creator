/**
 * "Topics" tab of the prompt navigator — the chat's table of contents from the
 * chat-topics labeller: topics in order of their first exchange (title, synopsis,
 * then that topic's prompt rows), and the prompts not yet labelled at the bottom.
 * Fetches on every mount (useChatToc); no store slices or events.
 */

import { Button } from '../ui';
import { useTranslation } from '../../i18n';
import { NavigatorRow, type PromptRailItem } from './PromptRail';
import { useChatToc, type ChatTocReason } from './useChatToc';

interface Props {
  items: PromptRailItem[];
  sessionId: string;
  currentId: string | null;
  hoverId: string | null;
  onHover: (id: string) => void;
  onPick: (id: string, viaKeyboard: boolean) => void;
}

interface TopicSection {
  key: string;
  title: string;
  synopsis: string;
  rows: PromptRailItem[];
}

const NOTE_CLASS = 'px-3 py-3 text-ui-xs text-text-dim';

const NO_TOC_NOTE = {
  recall_off: 'chatTocRecallOff',
  unindexed: 'chatTocUnindexed',
  memory_off: 'chatTocMemoryOff',
  pending: 'chatTocPending',
} as const satisfies Record<ChatTocReason, string>;

export function PromptRailTopics({ items, sessionId, currentId, hoverId, onHover, onPick }: Props) {
  const { t } = useTranslation();
  const { state, reload } = useChatToc(sessionId);

  if (state.status === 'loading') return <div className={NOTE_CLASS}>{t('loading')}</div>;
  if (state.status === 'error') {
    return (
      <div className={`${NOTE_CLASS} flex flex-col items-start gap-2`} role="alert">
        <span>{t('chatTocLoadFailed')}</span>
        <Button size="sm" variant="ghost" onClick={reload}>{t('retry')}</Button>
      </div>
    );
  }
  if (state.status === 'none') return <div className={NOTE_CLASS}>{t(NO_TOC_NOTE[state.reason])}</div>;

  const indexById = new Map(items.map((item, i) => [item.id, i]));
  // WHY skip unknown ids: an edited or deleted message stays in the row until
  // the labeller's verify drops it — it has no prompt to jump to.
  const sections: TopicSection[] = state.toc.topics
    .map((topic, i) => ({
      key: `topic-${i}`,
      title: topic.title,
      synopsis: topic.synopsis,
      rows: topic.message_ids
        .filter(id => indexById.has(id))
        .sort((a, b) => (indexById.get(a) ?? 0) - (indexById.get(b) ?? 0))
        .map(id => items[indexById.get(id) ?? 0]),
    }))
    .filter(section => section.rows.length > 0)
    .sort((a, b) => (indexById.get(a.rows[0].id) ?? 0) - (indexById.get(b.rows[0].id) ?? 0));
  const labelled = new Set(state.toc.labelled_ids);
  const unlabelled = items.filter(item => !labelled.has(item.id));
  if (unlabelled.length > 0) {
    sections.push({ key: 'unlabelled', title: t('chatTocUnlabelled', { count: unlabelled.length }), synopsis: '', rows: unlabelled });
  }

  return (
    <>
      {sections.map(section => (
        <section key={section.key} data-toc-topic={section.key}>
          <div className="px-3 pt-3 pb-1">
            <div className="text-ui-sm text-text font-medium">{section.title}</div>
            {section.synopsis && <div className="text-ui-xs text-text-dim mt-0.5">{section.synopsis}</div>}
          </div>
          {section.rows.map(item => (
            <NavigatorRow
              key={item.id}
              item={item}
              isCurrent={item.id === currentId}
              isTarget={item.id === hoverId && item.id !== currentId}
              onHover={() => onHover(item.id)}
              onPick={viaKeyboard => onPick(item.id, viaKeyboard)}
            />
          ))}
        </section>
      ))}
    </>
  );
}
