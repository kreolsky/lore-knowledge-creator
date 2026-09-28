/**
 * Presentational two-row list-row — shared inner content for the chat session
 * row (ChatRow), the note pill (NoteSessionCard), AND the reference pill
 * (RefCard).
 * // SYSTEM: list-row — single source of list-row typography + title→meta spacing
 *
 * Owns the title line + meta line rhythm so chat / notes / references cannot
 * drift apart (a previous bespoke RefCard title re-declared font-size/line-height
 * and drifted twice). Optional slots keep each consumer's extras without forking
 * the layout:
 *   - `actions`    : hover-reveal cluster overlaid on the title row (RefCard's
 *                    RowActions). Rendered inside a positioned wrapper so it
 *                    anchors to the title row only.
 *   - `metaExtra`  : extra nodes appended to the meta row (RefCard's StatusBadge
 *                    + file size).
 *   - `bodyOverride`: replaces the body span (RefCard's inline rename FieldInput)
 *                    while keeping the meta row intact.
 *
 * The body is a plain block span (no flex parent) so Tailwind's `line-clamp` /
 * `truncate` apply reliably — a flex wrapper around the clamped span previously
 * broke the clamp (notes overflowed past 2 lines).
 */

import type React from 'react';

interface ListRowProps {
  /** Primary clamped body text (chat title / note first-message text / ref title). */
  body: string;
  /** Replaces the body span (e.g. RefCard's inline rename input); keeps the meta row. */
  bodyOverride?: React.ReactNode;
  /** Number of body lines to clamp (1 = single-line truncate, like chat title). */
  bodyLines?: 1 | 2;
  /** Optional dim metadata row beneath the body (date [+ parent label]).
   *  Accepts ReactNode so RefCard can inject a colored scopeLabel plate inline. */
  meta?: React.ReactNode | null;
  /** Optional leading icon at the start of the meta row (e.g. discussion icon). */
  metaIcon?: React.ReactNode;
  /** Optional extra nodes appended to the meta row (e.g. RefCard StatusBadge + size). */
  metaExtra?: React.ReactNode;
  /** Render meta in full text color instead of dim (e.g. RefCard's open-reference emphasis). */
  metaEmphasized?: boolean;
  /** Optional hover-reveal action cluster overlaid on the title row (e.g. RefCard RowActions). */
  actions?: React.ReactNode;
  /** Optional double-click handler on the body (e.g. RefCard's rename trigger). */
  onBodyDoubleClick?: (e: React.MouseEvent) => void;
}

export function ListRow({
  body, bodyOverride, bodyLines = 1, meta, metaIcon, metaExtra, metaEmphasized = false, actions, onBodyDoubleClick,
}: ListRowProps) {
  const clamp = bodyLines === 2 ? 'line-clamp-2' : 'truncate';
  const metaCls = metaEmphasized ? 'text-text-muted' : 'text-text-dim';
  const titleEl = bodyOverride != null
    ? bodyOverride
    : (
      <span className={`block text-sm ${clamp}`} onDoubleClick={onBodyDoubleClick}>
        {body}
      </span>
    );
  const showMeta = (meta != null && meta !== '') || metaExtra != null;
  // actions is absolute-positioned; it needs a positioned ancestor scoped to the
  // title row (not the whole pill) so it overlays only the title. Wrap only then.
  const titleRow = actions != null
    ? <div className="relative min-w-0">{titleEl}{actions}</div>
    : titleEl;
  return (
    <>
      {titleRow}
      {showMeta && (
        <span className="flex items-center gap-1 mt-0.5">
          {metaIcon != null && <span className={`shrink-0 ${metaCls}`}>{metaIcon}</span>}
          {meta != null && meta !== '' && <span className={`block text-ui-xs ${metaCls} truncate`}>{meta}</span>}
          {metaExtra}
        </span>
      )}
    </>
  );
}
