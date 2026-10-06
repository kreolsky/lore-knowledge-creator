/**
 * ARCH: Closed props — no className/style passthrough to prevent style drift.
 * variant="dashed" replaces the old .panel-add-btn class.
 */

import { forwardRef } from 'react';

interface ButtonProps {
  variant?: 'primary' | 'ghost' | 'ghost-note' | 'yellow' | 'dashed' | 'danger' | 'subtle' | 'highlight' | 'composer';
  size?: 'sm' | 'md' | 'md-square' | 'lg' | 'lg-square';
  danger?: boolean;
  fullWidth?: boolean;
  onClick?: React.MouseEventHandler<HTMLButtonElement>;
  onMouseLeave?: React.MouseEventHandler<HTMLButtonElement>;
  disabled?: boolean;
  type?: 'button' | 'submit' | 'reset';
  title?: string;
  children: React.ReactNode;
}

// WHY: a button stretched by its container (grid column, w-full) centres its
// label; an intrinsic-width one is unaffected. fullWidth keeps justify-between.
const base =
  'inline-flex items-center gap-1.5 border-none cursor-pointer font-inherit text-ui-base font-medium transition-all duration-150 whitespace-nowrap disabled:opacity-40 disabled:cursor-default';

const variants: Record<string, string> = {
  default: 'bg-transparent text-text-muted',
  primary:
    'bg-accent text-white hover:brightness-108',
  ghost: 'bg-transparent text-text-muted hover:bg-surface3 hover:text-text',
  // see SYSTEM: note-chat — darker-yellow hover for controls inside a note thread.
  'ghost-note': 'bg-transparent text-text-muted hover:bg-[var(--sticky-yellow-dark)] hover:text-text',
  // see SYSTEM: note-chat — sticky-yellow pair (idle light → hover dark), same
  // tokens as anchored note pills / note links; theme-aware text like ghost-note.
  yellow: 'bg-[var(--sticky-yellow-light)] text-text-muted hover:bg-[var(--sticky-yellow-dark)] hover:text-text',
  danger:
    'bg-red text-white hover:brightness-108',
  subtle: 'bg-surface2 text-text-muted hover:bg-[var(--sticky-yellow-light)] hover:text-text',
  // SYSTEM: ui-primitives — composer selectors (model / mode / system-prompt).
  // ARCH: darker-than-field in BOTH themes via --composer-control-bg (index.css):
  // light uses surface2 (#dbd8d2 < surface), dark uses bg (#1e1e1e < surface).
  composer: 'bg-[var(--composer-control-bg)] text-text-muted hover:bg-surface3 hover:text-text',
  highlight:
    'bg-[var(--color-highlight-agent)] text-white hover:brightness-108',
  dashed:
    // text-ui-base = the document-row font (13px, index.css .doc-item): the
    // dashed button is a tree row action ("Add document", section-shell back
    // button) and must match. Token, not a px literal (styling gate).
    'flex items-center gap-1.5 w-full mb-1 py-2 px-2.5 border border-dashed border-border bg-transparent text-text-muted text-ui-base hover:border-accent hover:text-accent hover:bg-accent-soft',
};

const sizes: Record<string, string> = {
  md: 'h-[30px] px-3',
  // Icon-only square at the md height (compact-layout chat send).
  'md-square': 'h-[30px] w-[30px] p-0',
  sm: 'text-xs h-[22px] px-2.5',
  // lg = FieldInput's pinned height (h-[37px], FieldInput.tsx) — for a button
  // in the same row as an input (cabinet / admin forms). Never the default.
  lg: 'h-[37px] px-3 text-sm',
  // Icon-only square at the same input-row height (no className passthrough,
  // so the square is a size, not a class).
  'lg-square': 'h-[37px] w-[37px] p-0 justify-center text-sm',
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button({ variant, size = 'md', danger, fullWidth, children, ...rest }, ref) {
    const cls = variant === 'dashed'
      ? `${variants.dashed} cursor-pointer font-inherit transition-all duration-150`
      : `${base} ${variants[variant ?? 'default']} ${sizes[size]}${danger ? ' text-red' : ''}${fullWidth ? ' w-full justify-between' : ' justify-center'}`;

    return (
      <button ref={ref} className={cls} {...rest}>
        {children}
      </button>
    );
  },
);
