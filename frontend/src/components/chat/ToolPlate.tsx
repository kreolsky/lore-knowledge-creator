/** Collapsible tool-call plate — a generalization of ReasoningWidget.
 *
 * The header carries a tool icon + name (e.g. `search_materials`,
 * `edit_document`) instead of "Reasoning"; the expanded body is arbitrary
 * children. Markup/classes are identical to ReasoningWidget so the two plate
 * kinds are visually indistinguishable in a chat.
 *
 * Expand behavior mirrors the former ReasoningWidget: `defaultExpanded` sets the
 * initial open state; when `autoCollapse` is set the plate collapses once its
 * collapse signal goes true. By default the signal is "stream ended" (`!isStreaming`),
 * but a caller can override it with `collapseWhen` — e.g. the reasoning plate
 * collapses when the answer's content arrives (`collapseWhen={hasContent}`) rather
 * than waiting for stream end. A manual click overrides the auto effect for the
 * rest of the plate's life (manualRef). An EXPANDED plate also collapses on a
 * click anywhere in its body (guarded: interactive elements and text selection
 * pass through); expanding is still header-only.
 *
 * `tone` opts the plate into a distinct
 * visual treatment for a failed/noop agent step so the chip is not silently
 * rendered as a neutral success (`?`/`(?)` masked every failure before). `failed`
 * gets an amber left-border + amber header (No-silent-degradation); `noop` gets a
 * muted opacity. `border-radius: 0` preserved.
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
  /** Always-visible footer slot rendered inside the outer container, AFTER the
   *  collapsible body so collapsing the body does NOT hide it (e.g. the proposal
   *  Apply button row). Same bg-surface2 as the plate, so it reads as one block. */
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
  // failed: amber left-border + amber header text. noop: muted opacity.
  // border-radius stays 0 (the project-wide rule; no rounded-* here).
  const containerTone = tone === 'failed'
    ? 'bg-surface2 border-l-2 border-amber-500'
    : tone === 'noop'
      ? 'bg-surface2 opacity-70'
      : 'bg-surface2';
  const titleTone = tone === 'failed'
    ? 'text-amber-400'
    : tone === 'noop'
      ? 'text-text-muted'
      : 'text-text';

  // Click anywhere on the EXPANDED plate (header + body) collapses it. Only the
  // collapse direction lives here — expanding stays on the header button so a
  // collapsed chip does not open from stray body-area clicks. Guards:
  // interactive descendants (links, code-copy buttons, proposal Apply buttons,
  // inputs) keep their own action; an active text selection (drag-select fires
  // click on mouseup) must not collapse. Footer is OUTSIDE this container, so
  // its buttons are never collapse targets.
  const handleContainerClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!expanded) return;
    if ((e.target as HTMLElement).closest('a, button, input, textarea, select, [contenteditable="true"]')) return;
    if (window.getSelection()?.toString()) return;
    manualRef.current = true;
    setExpanded(false);
  };

  return (
    <div className="my-1">
      <div
        className={`${containerTone} text-text`}
        onClick={handleContainerClick}
      >
        {/* Custom full-width disclosure header — the closed-prop Button cannot express this layout. */}
        {/* eslint-disable-next-line react/forbid-elements */}
        <button
          type="button"
          onClick={() => { manualRef.current = true; setExpanded(e => !e); }}
          className="flex items-center justify-between gap-1 w-full px-2 py-1 text-left cursor-pointer bg-transparent"
          aria-label={ariaLabel}
          aria-expanded={expanded}
        >
          <span className="flex items-center gap-1 min-w-0 flex-1">
            {icon}
            <span className={`font-medium ${titleTone} min-w-0`}>{title}</span>
          </span>
          {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </button>
        {expanded && (
          <div className="px-2 pb-2 text-text-muted">
            {children}
          </div>
        )}
      </div>
      {/* Footer sits OUTSIDE the bg-surface2 box so its background is transparent
          (reveals the chat panel behind), but still inside the outer wrapper so it
          stays flush against the plate body — no gap. Rendered after the collapsible
          body block, so collapsing the body does NOT hide it (footer-collapse invariant). */}
      {footer && (
        <div className="px-2 py-1 text-text">
          {footer}
        </div>
      )}
    </div>
  );
}
