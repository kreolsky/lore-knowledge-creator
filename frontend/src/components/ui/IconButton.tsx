/**
 * ARCH: Closed props — no className/style passthrough.
 * size="sm" (22px) for note cards, "md" (28px, default) elsewhere.
 * filled — solid background with white text (e.g. armed-delete confirmation).
 */

interface IconButtonProps {
  size?: 'sm' | 'md';
  danger?: boolean;
  filled?: boolean;
  color?: 'red' | 'green' | 'accent';
  theme?: 'note';
  className?: string;
  onClick?: React.MouseEventHandler<HTMLButtonElement>;
  onMouseLeave?: React.MouseEventHandler<HTMLButtonElement>;
  disabled?: boolean;
  title?: string;
  'aria-label'?: string;
  children: React.ReactNode;
}

const baseLayout =
  'border-none cursor-pointer flex items-center justify-center transition-all duration-150';
const baseIdle = 'bg-transparent text-text-muted hover:bg-surface3 hover:text-text';
// see SYSTEM: note-chat — darker-yellow hover for controls inside a note thread,
// matching the sticky-note bg instead of the app-wide gray hover.
const baseIdleNote = 'bg-transparent text-text-muted hover:bg-[var(--sticky-yellow-dark)] hover:text-text';

const sizes: Record<string, string> = {
  md: 'w-[30px] h-[30px]',
  sm: 'w-[22px] h-[22px]',
};

const colors: Record<string, string> = {
  red: 'text-red hover:text-red',
  green: 'text-green hover:text-green',
};

const filledColors: Record<string, string> = {
  red: 'bg-red text-white hover:bg-red hover:text-white',
  accent: 'bg-accent text-white hover:bg-accent hover:text-white',
};

export function IconButton({ size = 'md', danger, filled, color, theme, className, children, ...rest }: IconButtonProps) {
  const idle = theme === 'note' ? baseIdleNote : baseIdle;
  const resolvedColor = color ?? (danger ? 'red' : '');
  const variant = filled && resolvedColor ? filledColors[resolvedColor] ?? idle : resolvedColor ? colors[resolvedColor] + ' ' + idle : idle;
  const cls = [baseLayout, sizes[size], variant, className].filter(Boolean).join(' ');
  return (
    <button type="button" className={cls} {...rest}>
      {children}
    </button>
  );
}
