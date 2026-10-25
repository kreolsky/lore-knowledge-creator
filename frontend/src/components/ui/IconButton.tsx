/**
 * ARCH: Closed props — no className/style passthrough.
 * size="sm" (22px) for note cards, "md" (28px, default) elsewhere.
 * filled — solid background with white text (e.g. armed-delete confirmation).
 * pressed — a toggle in its ON state: held at the hover background, aria-pressed set.
 * pressed + color — the ON state takes that color's tint instead (green = privacy mode; OFF stays plain).
 */

interface IconButtonProps {
  size?: 'sm' | 'md';
  danger?: boolean;
  filled?: boolean;
  pressed?: boolean;
  color?: 'red' | 'green' | 'accent' | 'amber';
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
const baseIdleNote = 'bg-transparent text-text-muted hover:bg-[var(--warm-strong)] hover:text-text';

// A pressed toggle holds the hover look so ON reads apart from OFF at rest.
const pressedIdle = 'bg-surface3 text-text hover:bg-surface3 hover:text-text';
const pressedIdleNote = 'bg-[var(--warm-strong)] text-text hover:bg-[var(--warm-strong)] hover:text-text';

// A colored pressed toggle replaces the gray hold entirely, so the two text colors never compete.
const pressedColors: Record<string, string> = {
  green: 'bg-green/15 text-green hover:bg-green/25 hover:text-green',
};

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

export function IconButton({ size = 'md', danger, filled, pressed, color, theme, className, children, ...rest }: IconButtonProps) {
  const pressedTint = pressed && color ? pressedColors[color] : undefined;
  const idle = pressedTint ?? (pressed
    ? (theme === 'note' ? pressedIdleNote : pressedIdle)
    : (theme === 'note' ? baseIdleNote : baseIdle));
  const resolvedColor = pressedTint ? '' : color ?? (danger ? 'red' : '');
  const variant = filled && resolvedColor ? filledColors[resolvedColor] ?? idle : colors[resolvedColor] ? colors[resolvedColor] + ' ' + idle : idle;
  const cls = [baseLayout, sizes[size], variant, className].filter(Boolean).join(' ');
  return (
    <button type="button" className={cls} aria-pressed={pressed} {...rest}>
      {children}
    </button>
  );
}
