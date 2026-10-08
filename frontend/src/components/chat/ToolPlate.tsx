/** Collapsible chip — every process block in a chat turn (tool call,
 * reasoning, verdict, image run) renders through it.
 *
 * Layout: a transparent header row (icon → title → chevron, the title+chevron
 * the only button) over one `bg-surface2` body box capped at the dsh group
 * height, so a chip reads as a line of the answer until it is opened.
 *
 * Expand behavior: `defaultExpanded` sets the initial open state; when
 * `autoCollapse` is set the plate collapses once its collapse signal goes true.
 * By default the signal is "stream ended" (`!isStreaming`), but a caller can
 * override it with `collapseWhen` — e.g. the reasoning plate collapses when the
 * answer's content arrives (`collapseWhen={hasContent}`). A manual click
 * overrides the auto effect for the rest of the plate's life (manualRef). An
 * EXPANDED plate also collapses on a click anywhere in its body (guarded:
 * interactive elements and text selection pass through); expanding is
 * title-only.
 *
 * `tone` gives a failed/noop agent step a distinct look so the chip is not
 * rendered as a neutral success: `failed` ⇒ amber left-border on the body +
 * amber header icon and title (No-silent-degradation); `noop` ⇒ muted opacity.
 * `border-radius: 0` preserved.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { ChevronRight, ChevronDown } from 'lucide-react';

export interface Props {
  icon: ReactNode;
  /**
   * The header title is a ReactNode (not a plain string)
   * so a structured header can render — e.g. the search chip's `[icon] label
   * [query truncate] [count shrink-0]` flex row, where the hit count is a protected
   * zone that must stay visible even when CSS ellipsizes the query. A plain-string
   * caller (every other chip) interpolates unchanged via `<span>{title}</span>`.
   * `ariaLabel` stays a separate `string` prop for a11y (screen readers must not
   * announce a structured node).
   */
  title: ReactNode;
  defaultExpanded?: boolean;
  autoCollapse?: boolean;
  isStreaming?: boolean;
  /** Override auto-collapse trigger: when true (and autoCollapse set, no manual
   *  toggle yet) the plate collapses. Defaults to `!isStreaming` (collapse on
   *  stream end). The reasoning plate passes `hasContent` so it collapses as soon
   *  as the answer begins. */
  collapseWhen?: boolean;
  /** Optional tone for the failed/noop
   *  agent-step chip. `failed` ⇒ amber accent; `noop` ⇒ muted. Absent (or
   *  undefined) ⇒ neutral (the default surface). */
  tone?: 'failed' | 'noop';
  /** Localized accessible label for the
   *  disclosure header. When set, overrides the visible `title` text for screen
   *  readers (which would otherwise announce the technical tool name). Used to
   *  surface the i18n-localized failed/noop outcome labels without changing the
   *  visible chip text (which stays server-driven for the audit record). */
  ariaLabel?: string;
  children: ReactNode;
  /** Group plate: the body is other chips, so it gets no background, no
   *  height cap (each member keeps its own — no nested scroll) and no
   *  body-click collapse (a click in a member's body must fold only that
   *  member). Members sit indented; a full-width rule closes the group. */
  bare?: boolean;
  /** Always-visible footer slot rendered AFTER the collapsible body so
   *  collapsing the body does NOT hide it (e.g. the verdict buttons row).
   *  Transparent, like the header. */
  footer?: ReactNode;
}

export function ToolPlate({
  icon,
  title,
  defaultExpanded = false,
  autoCollapse = false,
  isStreaming = false,
  collapseWhen,
  tone,
  ariaLabel,
  children,
  bare = false,
  footer,
}: Props) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  // Once the user clicks, the auto-collapse effect no longer fires.
  const manualRef = useRef(false);

  // The collapse signal: an explicit `collapseWhen` if provided, else `!isStreaming`.
  const trigger = collapseWhen !== undefined ? collapseWhen : !isStreaming;

  useEffect(() => {
    if (autoCollapse && !manualRef.current && trigger) setExpanded(false);
  }, [autoCollapse, trigger]);

  // Tone → Tailwind utilities (no new CSS class — utilities only, per styling.md).
  // failed: amber left-border on the BODY + amber header icon/title. noop: the
  // whole plate muted. border-radius stays 0 (the project-wide rule; no rounded-*).
  const plateTone = tone === 'noop' ? 'opacity-70' : '';
  const bodyTone = tone === 'failed' ? 'border-l-2 border-amber-500' : '';
  const iconTone = tone === 'failed' ? 'text-amber-400' : '';
  // A group plate is closed by a full-width rule: under its header while
  // collapsed, under its last member while expanded. A failed group: amber.
  const groupRule = `border-b ${tone === 'failed' ? 'border-amber-500' : 'border-border'}`;
  const titleTone = tone === 'failed'
    ? 'text-amber-400'
    : tone === 'noop'
      ? 'text-text-muted'
      : 'text-text';

  // Click anywhere in the EXPANDED body collapses it. Only the collapse
  // direction lives here — expanding stays on the title button so a collapsed
  // chip does not open from stray clicks. Guards: interactive descendants
  // (links, code-copy buttons, proposal Apply buttons, inputs) keep their own
  // action; an active text selection (drag-select fires click on mouseup) must
  // not collapse. Footer is OUTSIDE the body, so its buttons are never targets.
  const handleBodyClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest('a, button, input, textarea, select, [contenteditable="true"]')) return;
    if (window.getSelection()?.toString()) return;
    manualRef.current = true;
    setExpanded(false);
  };

  return (
    <div className={`my-1 ${plateTone}`}>
      {/* Header row: transparent, flush with the answer text (no side padding).
          Only title + chevron is the click target — the icon and the empty rest
          of the row are not. The button is inline-flex min-w-0 max-w-full so a
          long title still ellipsizes instead of wrapping the row. */}
      <div className={`flex items-center gap-1 min-w-0 py-1 text-text ${bare && !expanded ? groupRule : ''}`}>
        <span className={`flex items-center shrink-0 ${iconTone}`}>{icon}</span>
        {/* Custom inline disclosure header — the closed-prop Button cannot express this layout. */}
        {/* eslint-disable-next-line react/forbid-elements */}
        <button
          type="button"
          onClick={() => { manualRef.current = true; setExpanded(e => !e); }}
          className="inline-flex items-center gap-1 min-w-0 max-w-full text-left cursor-pointer bg-transparent"
          aria-label={ariaLabel}
          aria-expanded={expanded}
        >
          <span className={`flex font-medium ${titleTone} min-w-0`}>{title}</span>
          {expanded
            ? <ChevronDown size={13} className="shrink-0" />
            : <ChevronRight size={13} className="shrink-0" />}
        </button>
      </div>
      {expanded && (bare
        ? <div className={`pl-4 pb-1 ${groupRule}`}>{children}</div>
        : (
          <div
            className={`bg-surface2 ${bodyTone} p-2 text-text-muted max-h-[min(400px,50vh)] overflow-auto`}
            onClick={handleBodyClick}
          >
            {children}
          </div>
        ))}
      {/* Footer sits OUTSIDE the body box so its background is transparent
          (reveals the chat panel behind), flush against the plate. Rendered
          after the collapsible body, so collapsing the body does NOT hide it
          (footer-collapse invariant). */}
      {footer && (
        <div className="px-2 py-1 text-text">
          {footer}
        </div>
      )}
    </div>
  );
}
