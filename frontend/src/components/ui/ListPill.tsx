/**
 * ListPill — shared right-panel pill chrome for chat / references / notes / search.
 * // SYSTEM: list-pill — single source of pill chrome (variants, active color, hover reveal)
 *
 * Replaces the ChatRow bare div, NoteCard, and the RefCard .card shell. Renders an
 * interactive (role=button) borderless pill; consumers pass their inner content
 * (usually <ListRow>) as children. The root carries `group` so RowActions / the note
 * delete button reveal on hover.
 *
 * Variants + active colors live in ListPill.module.css — see its header for the
 * yellow/anchorless/backlink invariants and the delete-btn backdrop rule.
 */

import { memo } from 'react';
import type React from 'react';
import styles from './ListPill.module.css';

export type ListPillVariant = 'plain' | 'anchored' | 'highlight' | 'anchorless' | 'backlink';
export type ListPillActiveColor = 'accent' | 'blue';

interface ListPillProps {
  variant?: ListPillVariant;
  active?: boolean;
  /** Active fill color when variant is 'plain': chat purple 'accent' or reference 'blue'. */
  activeColor?: ListPillActiveColor;
  /** see SYSTEM: inbox — this object arrived from OUTSIDE flagged for the viewer.
   * Paints the sticky-yellow inbox mark OVER the variant fill (the anchored
   * yellow family; the mark is the dark shade, matching the tab tint). Clearing
   * happens on open, so an active+unread overlap is a transient failure state. */
  unread?: boolean;
  id?: string;
  clickable?: boolean;
  tabIndex?: number;
  className?: string;
  style?: React.CSSProperties;
  onClick?: (e: React.MouseEvent) => void;
  onMouseEnter?: (e: React.MouseEvent) => void;
  onMouseLeave?: (e: React.MouseEvent) => void;
  children: React.ReactNode;
  /** Drag-reorder row attributes (see useSiblingDragReorder): rendered verbatim
   * on the root so the drag hook can find rows by id/group. */
  [key: `data-${string}`]: string | undefined;
}

// WHY memo: right-panel lists re-render on any list mutation; pill props are stable by row identity.
export const ListPill = memo(function ListPill({
  variant = 'plain',
  active = false,
  activeColor = 'accent',
  unread = false,
  id,
  clickable = true,
  tabIndex,
  className,
  style,
  onClick,
  onMouseEnter,
  onMouseLeave,
  children,
  ...dataAttrs
}: ListPillProps) {
  const activeCls = active
    ? variant === 'plain'
      ? activeColor === 'blue' ? styles.activeBlue : styles.activeAccent
      : ''
    : '';
  const cls = [
    styles.pill,
    styles[variant],
    activeCls,
    unread ? styles.unread : '',
    clickable ? 'cursor-pointer' : 'cursor-default',
    'group',
    className,
  ].filter(Boolean).join(' ');

  return (
    <div
      id={id}
      role="button"
      tabIndex={tabIndex ?? 0}
      className={cls}
      style={style}
      onClick={onClick}
      onKeyDown={e => {
        // Only activate when the key event originates on the pill itself — inner
        // buttons (RowActions, note delete) bubble their Enter/Space here, and we
        // must not double-fire onClick (select) on top of the button's own action.
        if (clickable && e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) {
          e.preventDefault();
          onClick?.(e as unknown as React.MouseEvent);
        }
      }}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      {...dataAttrs}
    >
      {children}
    </div>
  );
});
