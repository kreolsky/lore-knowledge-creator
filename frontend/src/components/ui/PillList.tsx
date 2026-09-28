/**
 * PillList — shared right-panel list container for chat / notes / references / search.
 * // SYSTEM: pill-list — single source of list-container spacing (outer padding + inter-pill gap)
 *
 * Container-level counterpart to ListPill: ListPill owns the pill chrome, PillList owns
 * the spacing around and between pills. All four right-panel lists render their pills
 * inside a PillList so container spacing can't drift again.
 *
 * Spacing lives in the `.pill-list` CSS class (index.css): 8px side / 8px top / 12px
 * bottom outer padding, inter-pill gap via the `--pill-gap` CSS variable (:root).
 *
 * INVARIANT: consumers MUST NOT override padding/gap via `className` — that is the exact  Why: PillList centralizes spacing; allowing padding/gap overrides via className would re-introduce the drift it prevents.
 * drift this component exists to prevent. The spacing-related concerns that a list could
 * legitimately need are exposed as dedicated boolean props (`reserveFooter`) so they can't
 * silently re-introduce pixel drift. `className` is for NON-spacing layout additions only
 * (e.g. `min-h-0` for flexbox min-height), never `px-`/`py-`/`p-`/`gap-`/`pt-`/`pb-`.
 */
import { forwardRef } from 'react';
import type React from 'react';

interface PillListProps {
  /**
   * Non-spacing layout additions only (e.g. `min-h-0`).
   * INVARIANT: must NEVER carry padding/gap overrides (`px-`, `py-`, `p-`, `gap-`, `pt-`, `pb-`).  Why: same anti-drift rule at the className level; padding/gap utilities are forbidden so consumers can't bypass the centralized spacing.
   */
  className?: string;
  style?: React.CSSProperties;
  /** Reserve bottom clearance (pb-14) for an absolutely-positioned footer overlay (chat list). */
  reserveFooter?: boolean;
  children: React.ReactNode;
}

export const PillList = forwardRef<HTMLDivElement, PillListProps>(function PillList(
  { className, style, reserveFooter = false, children },
  ref,
) {
  const cls = ['pill-list'];
  if (reserveFooter) cls.push('pb-14');
  if (className) cls.push(className);
  return (
    <div ref={ref} className={cls.join(' ')} style={style}>
      {children}
    </div>
  );
});
