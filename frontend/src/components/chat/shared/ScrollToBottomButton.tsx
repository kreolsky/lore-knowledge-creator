/** Round accent scroll-jump buttons shown while the chat is scrolled up: to the end, and up to the previous user message. */
// see SYSTEM: chat-composer — rendered in the composer's overlay column above the queue.
// WHY: round despite the project-wide no-rounded-corners rule — operator decision for
// these floating jump controls — to the end and to the previous own message (they
// mirror the familiar chat "scroll down" affordance).

import { ArrowDown, ArrowUp } from 'lucide-react';
import { IconButton } from '../../ui';
import { useTranslation } from '../../../i18n';

interface Props {
  onClick: () => void;
}

export function ScrollToBottomButton({ onClick }: Props) {
  const { t } = useTranslation();
  return (
    <IconButton
      color="accent"
      filled
      className="scroll-to-bottom-btn shadow-md pointer-events-auto"
      onClick={onClick}
      title={t('scrollToBottom')}
      aria-label={t('scrollToBottom')}
    >
      <ArrowDown size={16} />
    </IconButton>
  );
}

export function ScrollToPrevUserButton({ onClick }: Props) {
  const { t } = useTranslation();
  return (
    <IconButton
      color="accent"
      filled
      className="scroll-to-bottom-btn shadow-md pointer-events-auto"
      onClick={onClick}
      title={t('scrollToPrevUserMessage')}
      aria-label={t('scrollToPrevUserMessage')}
    >
      <ArrowUp size={16} />
    </IconButton>
  );
}
