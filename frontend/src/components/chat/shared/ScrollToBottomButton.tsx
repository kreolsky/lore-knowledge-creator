/** Round accent "jump to the end" button shown while the chat is scrolled up. */
// see SYSTEM: chat-composer — rendered in the composer's overlay column above the queue.
// WHY: round despite the project-wide no-rounded-corners rule — operator decision for
// this one floating control (it mirrors the familiar chat "scroll down" affordance).

import { ArrowDown } from 'lucide-react';
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
