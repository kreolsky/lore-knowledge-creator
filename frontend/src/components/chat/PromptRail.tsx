/**
 * Prompt rail — one tick per user message of the active branch on the message
 * list's right edge, plus a navigator panel (prompt + answer preview per row)
 * opened by hover (mouse) or tap (touch/pen). Clicking a tick or a row jumps to
 * that prompt through `onJump`; the rail itself owns only its "current" state.
 * No store slices or events — MessageList feeds it props.
 */

import { useCallback, useEffect, useRef, useState, type PointerEvent, type RefObject } from 'react';
import { useClickOutside } from '../../hooks/useClickOutside';
import { useTranslation } from '../../i18n';
import { userLanding, userRows } from './scroll-geometry';

export interface PromptRailItem {
  id: string;
  prompt: string;
  answer: string;
}

interface Props {
  items: PromptRailItem[];
  containerRef: RefObject<HTMLDivElement | null>;
  onJump: (id: string) => void;
}

// Pointer travel time from the rail to the panel before a mouse leave closes it.
const HOVER_CLOSE_MS = 150;
// A landing within this many px of scrollTop counts as reached — scrollTop reads back rounded.
const CURRENT_SLACK = 2;
// WHY no border, the deep shadow: matches the link hover preview (LinkPreviewPopup).
const PANEL_CLASS = 'bg-surface shadow-[0_8px_32px_rgba(0,0,0,0.25)] z-50';

/** First non-empty line of a prompt — the row title. */
export function promptTitle(text: string): string {
  return text.split('\n').map(line => line.trim()).find(Boolean) ?? '';
}

/** An answer as one line of prose: leading block markers and inline emphasis dropped. */
export function answerPreview(text: string): string {
  return text
    .replace(/^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)+/gm, '')
    .replace(/[*_`~]+/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

// The last prompt whose top is at or above the viewport top; the first when none is.
function currentPromptId(el: HTMLElement, items: PromptRailItem[]): string | null {
  const rows = userRows(el, new Set(items.map(item => item.id)));
  let current = rows[0]?.dataset.messageId ?? null;
  for (const row of rows) {
    if (userLanding(el, row) > el.scrollTop + CURRENT_SLACK) break;
    current = row.dataset.messageId ?? current;
  }
  return current;
}

export function PromptRail({ items, containerRef, onJump }: Props) {
  const { t } = useTranslation();
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const pointerRef = useRef<string>('mouse');
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());

  const measure = useCallback(() => {
    const el = containerRef.current;
    if (el) setCurrentId(currentPromptId(el, items));
  }, [containerRef, items]);

  // WHY the rail listens itself: a scroll must not re-render the message list,
  // only the rail (rAF-throttled — one measure per frame).
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    measure();
    let frame: number | null = null;
    const onScroll = () => {
      if (frame !== null) return;
      frame = requestAnimationFrame(() => { frame = null; measure(); });
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      el.removeEventListener('scroll', onScroll);
      if (frame !== null) cancelAnimationFrame(frame);
    };
  }, [containerRef, measure]);

  const cancelClose = useCallback(() => {
    if (closeTimerRef.current !== null) clearTimeout(closeTimerRef.current);
    closeTimerRef.current = null;
  }, []);
  useEffect(() => cancelClose, [cancelClose]);

  const close = useCallback(() => setOpen(false), []);
  useClickOutside(rootRef, close, open);

  useEffect(() => {
    if (open && currentId) rowRefs.current.get(currentId)?.scrollIntoView?.({ block: 'nearest' });
  }, [open, currentId]);

  if (items.length < 2) return null;

  const isMouse = () => pointerRef.current === 'mouse';

  const jump = (id: string, closeAfter: boolean) => {
    onJump(id);
    measure();
    if (closeAfter) setOpen(false);
  };

  const onPointerEnter = (e: PointerEvent) => {
    pointerRef.current = e.pointerType;
    if (e.pointerType !== 'mouse') return;
    cancelClose();
    setOpen(true);
  };
  const onPointerLeave = (e: PointerEvent) => {
    if (e.pointerType !== 'mouse') return;
    cancelClose();
    closeTimerRef.current = setTimeout(() => setOpen(false), HOVER_CLOSE_MS);
  };

  return (
    <div
      ref={rootRef}
      // WHY z-10: above dsh's sticky code-block banner (z-index 6) in the message list.
      className="absolute top-0 bottom-0 right-0 z-10 flex items-center pointer-events-none"
      onPointerEnter={onPointerEnter}
      onPointerLeave={onPointerLeave}
      onPointerDown={e => { pointerRef.current = e.pointerType; }}
    >
      {open && (
        <div
          className={`absolute right-full top-1/2 -translate-y-1/2 mr-1 w-72 max-h-[calc(100%-16px)] flex flex-col pointer-events-auto ${PANEL_CLASS}`}
          role="dialog"
          aria-label={t('promptNavigator')}
        >
          <div className="list-scroll overflow-y-auto min-h-0">
            {items.map(item => {
              const preview = answerPreview(item.answer);
              const isCurrent = item.id === currentId;
              return (
                <div
                  key={item.id}
                  ref={node => { if (node) rowRefs.current.set(item.id, node); else rowRefs.current.delete(item.id); }}
                  role="button"
                  tabIndex={0}
                  aria-current={isCurrent || undefined}
                  data-prompt-row={item.id}
                  onClick={() => jump(item.id, !isMouse())}
                  onKeyDown={e => { if (e.key === 'Enter') jump(item.id, false); }}
                  className={`px-3 py-2 cursor-pointer ${isCurrent ? 'bg-surface2' : 'hover:bg-surface2'}`}
                >
                  <div className={`truncate text-ui-sm ${isCurrent ? 'text-text' : 'text-text-muted'}`}>{promptTitle(item.prompt)}</div>
                  {preview && <div className="line-clamp-2 text-ui-xs text-text-dim mt-0.5">{preview}</div>}
                </div>
              );
            })}
          </div>
        </div>
      )}
      <div
        // WHY presentation: the ticks are a pointer shortcut; the keyboard path is the panel's rows.
        // WHY flush right: a cursor pushed against the screen edge must land on the rail.
        // WHY prompt-rail: rounded left corners by operator decision (see index.css).
        role="presentation"
        className="prompt-rail flex flex-col justify-center max-h-full px-1.5 py-2 bg-surface/40 cursor-pointer pointer-events-auto"
        data-prompt-rail
        onClick={() => { if (!isMouse()) setOpen(v => !v); }}
      >
        {items.map(item => {
          const isCurrent = item.id === currentId;
          return (
            <div
              key={item.id}
              role="presentation"
              data-prompt-tick={item.id}
              aria-current={isCurrent || undefined}
              className="flex items-center justify-end h-3 min-h-[4px] shrink w-5"
              onClick={e => {
                if (!isMouse()) return; // touch: the tap toggles the panel instead
                e.stopPropagation();
                jump(item.id, false);
              }}
            >
              <div className={`h-[3px] ${isCurrent ? 'w-5 bg-text' : 'w-3 bg-text-dim'}`} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
