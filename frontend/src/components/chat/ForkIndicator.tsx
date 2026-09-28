/** Fork navigation — shows "1/3 ◄►" when a message has siblings. */

import { ChevronLeft, ChevronRight } from 'lucide-react';
import { IconButton } from '../ui';
import { useTranslation } from '../../i18n';

interface Props {
  current: number;
  total: number;
  onPrev: () => void;
  onNext: () => void;
  /** Extra root classes (e.g. spacing). No default margin — the caller places it:
   * the assistant render stacks it above the bubble (mb-1), the user action row
   * aligns it with the buttons (no margin, or vertical centering breaks). */
  className?: string;
}

export function ForkIndicator({ current, total, onPrev, onNext, className = '' }: Props) {
  const { t } = useTranslation();
  return (
    <div className={`flex items-center gap-0.5 text-ui-xs text-text-dim ${className}`}>
      <IconButton size="sm" title={t('previous')} onClick={onPrev} disabled={current <= 1}>
        <ChevronLeft size={12} />
      </IconButton>
      <span>{current}/{total}</span>
      <IconButton size="sm" title={t('next')} onClick={onNext} disabled={current >= total}>
        <ChevronRight size={12} />
      </IconButton>
    </div>
  );
}
