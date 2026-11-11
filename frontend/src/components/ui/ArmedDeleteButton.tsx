/** Two-click delete: the first click arms (filled red), the second fires; leaving the button disarms. */
import { Trash2 } from 'lucide-react';
import { IconButton } from './IconButton';
import { useArmedAction } from '../../hooks/useArmedAction';
import { useTranslation } from '../../i18n';

const ICON_SIZE = { sm: 12, md: 14 } as const;

export function ArmedDeleteButton({ onFire, disabled, title, size = 'sm' }: {
  onFire: () => void;
  disabled?: boolean;
  title?: string;
  size?: 'sm' | 'md';
}) {
  const { t } = useTranslation();
  const action = useArmedAction();
  return (
    <IconButton
      size={size}
      danger
      filled={action.armed}
      onClick={() => action.handleClick(onFire)}
      onMouseLeave={action.disarm}
      disabled={disabled}
      title={title ?? t('delete')}
    >
      <Trash2 size={ICON_SIZE[size]} />
    </IconButton>
  );
}
