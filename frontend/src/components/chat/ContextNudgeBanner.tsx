/** ContextNudgeBanner — the "this chat has grown long" strip above the composer.
 *
 * Non-modal. ChatInput mounts it while the active chat has crossed more
 * thresholds than the person answered (k > context_nudge_level) and no turn is
 * streaming. Both buttons answer the nudge (PATCH context_nudge_level = k);
 * "New chat" also opens a fresh ghost. The copy says what carries over by the
 * persona's memory: ON — the chat is saved and its key points reach the next
 * chats on their own; OFF — a new chat starts from scratch.
 *
 * INVARIANT (project-wide): border-radius: 0 — no rounded corners anywhere.
 */
import { Button } from '../ui';
import { useTranslation } from '../../i18n';

interface Props {
  memoryOn: boolean;
  onNewChat: () => void;
  onContinue: () => void;
}

export function ContextNudgeBanner({ memoryOn, onNewChat, onContinue }: Props) {
  const { t } = useTranslation();
  return (
    // role="status": a polite live region — the strip is announced when it
    // appears, without stealing focus from the composer.
    <div
      role="status"
      data-testid="context-nudge-banner"
      className="mt-1.5 mb-px flex flex-wrap items-center gap-2 px-2 py-1 border border-amber/40 bg-amber/5"
    >
      <span className="min-w-0 flex-1 text-ui-sm text-amber">
        {t('chatContextNudgeTitle')}{' '}
        {memoryOn ? t('chatContextNudgeCarryMemory') : t('chatContextNudgeCarryNone')}
      </span>
      <span className="flex items-center gap-1 shrink-0">
        <Button variant="primary" onClick={onNewChat}>
          {t('chatNew')}
        </Button>
        <Button variant="ghost" onClick={onContinue}>
          {t('chatContextNudgeContinue')}
        </Button>
      </span>
    </div>
  );
}
