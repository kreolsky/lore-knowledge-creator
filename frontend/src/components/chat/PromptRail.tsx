/**
 * Prompt rail — one tick per user message of the active branch on the message
 * list's right edge, capped at three quarters of the list height: ticks that do not fit at
 * full pitch are compressed into a thinned overview, with a window centred on the
 * hovered tick (at rest, on the current prompt): spacing tapers from full at its
 * centre to the overview's at its edges, tick thickness and length fall as a bell. Plus a navigator panel (prompt + answer preview per row)
 * opened by hover (mouse) or tap (touch/pen). Clicking a tick or a row jumps to
 * that prompt through `onJump`; the rail itself owns only its "current" state.
 * The navigator has two tabs: every prompt in order, and the chat's topics
 * (PromptRailTopics). No store slices or events — MessageList feeds it props.
 */

import { useCallback, useEffect, useRef, useState, type PointerEvent, type RefObject } from 'react';
import { useClickOutside } from '../../hooks/useClickOutside';
import { useTranslation } from '../../i18n';
import { SegmentedControl } from '../ui';
import { PromptRailTopics } from './PromptRailTopics';
import {
  HAIRLINE, LENS_RADIUS, PEAK_THICKNESS, centredStart, followWindow, lensBell, lensFor,
  segmentAt, tickAt, tickGeometry, windowLayout,
} from './prompt-rail-geometry';
import { userLanding, userRows } from './scroll-geometry';

export interface PromptRailItem {
  id: string;
  prompt: string;
  answer: string;
}

interface Props {
  items: PromptRailItem[];
  containerRef: RefObject<HTMLDivElement | null>;
  sessionId: string | null;
  onJump: (id: string) => void;
}

type NavigatorTab = 'prompts' | 'topics';
// WHY localStorage: the last-used tab is a per-viewer convenience, not state.
const TAB_STORAGE_KEY = 'lore.promptRail.tab';

function readTab(): NavigatorTab {
  try {
    return localStorage.getItem(TAB_STORAGE_KEY) === 'topics' ? 'topics' : 'prompts';
  } catch {
    return 'prompts';
  }
}

function writeTab(tab: NavigatorTab): void {
  try {
    localStorage.setItem(TAB_STORAGE_KEY, tab);
  } catch {
    // Storage blocked (private window): the tab still switches, it just is not remembered.
  }
}

// Pointer travel time from the rail to the panel before a mouse leave closes it.
const HOVER_CLOSE_MS = 150;
// A landing within this many px of scrollTop counts as reached — scrollTop reads back rounded.
const CURRENT_SLACK = 2;
// The rail never grows past this share of the message list height.
const RAIL_MAX_SHARE = 0.75;
// The rail's vertical padding (py-2), top and bottom each.
const RAIL_PADDING = 8;
// The focus tick's length in CSS px; the bell falls from it to the overview's.
const PEAK_WIDTH = 24;
// Length of an overview tick, and the least the current tick is drawn at.
const BASE_WIDTH = 12;
const CURRENT_WIDTH = 20;
// With the navigator closed every tick, and the track, is drawn at this share of its length.
const REST_SCALE = 0.5;
// Share of the remaining distance the navigator list covers per frame, and the px it snaps within.
const LIST_EASE = 0.4;
const LIST_SNAP = 2;

/** devicePixelRatio, re-read on resize — browser zoom resizes the CSS viewport. */
function useDevicePixelRatio(): number {
  const [dpr, setDpr] = useState(() => window.devicePixelRatio || 1);
  useEffect(() => {
    const onResize = () => setDpr(window.devicePixelRatio || 1);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  return dpr;
}

/** Height of the message list area the rail sits in; 0 until measured. */
function useHeight(ref: RefObject<HTMLElement | null>, active: boolean): number {
  const [height, setHeight] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el || !active || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => setHeight(el.clientHeight));
    ro.observe(el);
    setHeight(el.clientHeight);
    return () => ro.disconnect();
  }, [ref, active]);
  return height;
}

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


/**
 * The list scrollTop that puts the pointer's y on the line `t` of the way from row a's
 * centre to row b's, clamped to the list's scroll range.
 * WHY interpolated, not per row: the list follows the pointer continuously and lines a
 * row up with the cursor exactly at its tick's centre — a per-tick jump of a whole row
 * (rows are ~5x a tick's pitch) read as jerks.
 */
export function listScrollTarget(list: HTMLElement, a: HTMLElement, b: HTMLElement, t: number, y: number): number {
  const listTop = list.getBoundingClientRect().top;
  const centre = (row: HTMLElement) => {
    const box = row.getBoundingClientRect();
    return box.top + box.height / 2 - listTop + list.scrollTop;
  };
  const line = centre(a) + (centre(b) - centre(a)) * t;
  return Math.min(list.scrollHeight - list.clientHeight, Math.max(0, line - (y - listTop)));
}

interface NavigatorRowProps {
  item: PromptRailItem;
  isCurrent: boolean;
  isTarget: boolean;
  rowRef?: (node: HTMLDivElement | null) => void;
  onHover: () => void;
  onPick: (viaKeyboard: boolean) => void;
}

/** One navigator row — the prompt's first line and a two-line answer preview; shared by both tabs. */
export function NavigatorRow({ item, isCurrent, isTarget, rowRef, onHover, onPick }: NavigatorRowProps) {
  const preview = answerPreview(item.answer);
  const rowTone = isCurrent ? 'bg-surface3' : isTarget ? 'bg-surface2' : '';
  return (
    <div
      ref={rowRef}
      role="button"
      tabIndex={0}
      aria-current={isCurrent || undefined}
      data-prompt-row={item.id}
      onPointerEnter={e => { if (e.pointerType === 'mouse') onHover(); }}
      onClick={() => onPick(false)}
      onKeyDown={e => { if (e.key === 'Enter') onPick(true); }}
      className={`px-3 py-2 cursor-pointer transition-colors duration-150 ${rowTone}`}
    >
      <div className={`truncate text-ui-sm ${isCurrent ? 'text-text' : 'text-text-muted'}`}>{promptTitle(item.prompt)}</div>
      {preview && <div className="line-clamp-2 text-ui-xs text-text-dim mt-0.5">{preview}</div>}
    </div>
  );
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

export function PromptRail({ items, containerRef, sessionId, onJump }: Props) {
  const { t } = useTranslation();
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<NavigatorTab>(readTab);
  // The jump target under the pointer (tick or row) — drawn apart from the current prompt.
  const [hoverId, setHoverId] = useState<string | null>(null);
  // The mouse is over the track: the window follows it without the glide.
  const [tracking, setTracking] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const pointerRef = useRef<string>('mouse');
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const listRef = useRef<HTMLDivElement>(null);
  const listGoalRef = useRef(0);
  const listFrameRef = useRef<number | null>(null);
  const dpr = useDevicePixelRatio();
  const tick = tickGeometry(dpr);
  const areaHeight = useHeight(rootRef, items.length >= 2);

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
  useEffect(() => () => { if (listFrameRef.current !== null) cancelAnimationFrame(listFrameRef.current); }, []);

  // WHY eased: a mouse reports in steps, and the list moves ~5x the pointer, so raw steps judder.
  const glideList = useCallback((goal: number) => {
    listGoalRef.current = goal;
    if (listFrameRef.current !== null) return;
    const step = () => {
      const list = listRef.current;
      const delta = listGoalRef.current - (list?.scrollTop ?? 0);
      if (!list || Math.abs(delta) < LIST_SNAP) {
        if (list) list.scrollTop = listGoalRef.current;
        listFrameRef.current = null;
        return;
      }
      list.scrollTop += delta * LIST_EASE;
      listFrameRef.current = requestAnimationFrame(step);
    };
    listFrameRef.current = requestAnimationFrame(step);
  }, []);

  const close = useCallback(() => setOpen(false), []);
  useClickOutside(rootRef, close, open);

  useEffect(() => {
    if (open && currentId) rowRefs.current.get(currentId)?.scrollIntoView?.({ block: 'nearest' });
  }, [open, currentId, tab]);

  if (items.length < 2) return null;

  const isMouse = () => pointerRef.current === 'mouse';

  const count = items.length;
  const fullHeight = count * tick.pitch;
  // Unmeasured area (0) leaves the rail at full pitch.
  const cap = areaHeight > 0
    ? Math.max(2 * tick.pitch, Math.floor(areaHeight * RAIL_MAX_SHARE) - 2 * RAIL_PADDING)
    : Infinity;
  const lensed = fullHeight > cap;
  const trackHeight = lensed ? cap : fullHeight;
  const lens = lensed ? lensFor(count, tick.pitch, trackHeight) : null;
  const indexOf = (id: string | null) => Math.max(0, items.findIndex(item => item.id === id));
  const focus = indexOf(hoverId ?? currentId);
  const windowStart = lens ? centredStart(count, lens, focus) : 0;
  const { centres, edges } = lens
    ? windowLayout(count, tick.pitch, lens, windowStart, focus)
    : {
      centres: Array.from({ length: count }, (_, i) => (i + 0.5) * tick.pitch),
      edges: Array.from({ length: count + 1 }, (_, i) => i * tick.pitch),
    };
  // Overview ticks drawn: every `stride`-th by global index, so the pattern holds as the window moves.
  const stride = lens ? lens.stride : 1;
  const inWindow = (i: number) => !lens || (i >= windowStart && i < windowStart + lens.size);
  const snap = (v: number) => Math.round(v * dpr) / dpr;
  // WHY glide only at rest: under the pointer a transition lags the lens behind the cursor.
  const glide = tracking ? '' : 'transition-[top,height] duration-150';
  const hairline = Math.max(1, Math.round(dpr)) / dpr;
  // The bell around the focus tick; an unlensed rail spreads it over LENS_RADIUS.
  // WHY stretched per side: at a rail end the window is clamped, so the bell reaches its far edge
  // instead of spending half its hump past the end.
  const bellAt = (i: number) => {
    if (!lens) return lensBell(Math.abs(i - focus), LENS_RADIUS);
    const radius = (lens.size - 1) / 2;
    const reach = i < focus ? focus - windowStart : windowStart + lens.size - 1 - focus;
    return reach === 0 ? 0 : lensBell(Math.abs(i - focus) * radius / reach, radius);
  };
  // INVARIANT: the current prompt's tick is always at least full thickness and current length (scaled at rest), in the window or not.
  // Why: it is "where I am" — operator decision, it must never thin out of sight.
  const thicknessOf = (i: number, isCurrent: boolean) => {
    const floor = isCurrent || !lens ? tick.thickness : hairline;
    if (lens && !inWindow(i)) return floor;
    const bell = HAIRLINE + (PEAK_THICKNESS - HAIRLINE) * bellAt(i);
    return Math.max(floor, Math.max(1, Math.round(bell * dpr)) / dpr);
  };
  // WHY shrunk at rest: the rail stays quiet beside the messages and grows when the pointer reaches it.
  // WHY proportional: the bell's hump keeps its shape at rest, only quieter.
  const scale = open ? 1 : REST_SCALE;
  const widthOf = (i: number, isCurrent: boolean) => {
    const bell = lens && !inWindow(i) ? 0 : bellAt(i);
    return Math.round(Math.max(isCurrent ? CURRENT_WIDTH : 0, Math.round(BASE_WIDTH + (PEAK_WIDTH - BASE_WIDTH) * bell)) * scale);
  };

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
    setHoverId(null);
    closeTimerRef.current = setTimeout(() => setOpen(false), HOVER_CLOSE_MS);
  };
  // Keyboard keeps the panel open; a tap closes it, a mouse leaves that to the pointer leaving.
  const pick = (id: string, viaKeyboard: boolean) => jump(id, !viaKeyboard && !isMouse());
  const switchTab = (value: string) => {
    const next: NavigatorTab = value === 'topics' ? 'topics' : 'prompts';
    setTab(next);
    writeTab(next);
  };
  // WHY the prompts tab without a session: the ToC is read by session id.
  const shownTab: NavigatorTab = sessionId ? tab : 'prompts';
  // WHY only from the track: scrolling the panel under a hovered row would slide rows under the pointer.
  const onTrackMove = (e: PointerEvent<HTMLDivElement>) => {
    if (e.pointerType !== 'mouse') return;
    const y = Math.min(trackHeight, Math.max(0, e.clientY - e.currentTarget.getBoundingClientRect().top));
    const followed = lens ? followWindow(count, tick.pitch, lens, focus, y) : null;
    const index = followed ? followed.index : tickAt(edges, y);
    setTracking(true);
    // The centres of the layout this move draws: the followed window's, not the one on screen.
    const drawn = lens && followed ? windowLayout(count, tick.pitch, lens, followed.start, followed.index).centres : centres;
    const { a, t } = segmentAt(drawn, y);
    const rowA = rowRefs.current.get(items[a].id);
    const rowB = rowRefs.current.get(items[a + 1].id);
    if (listRef.current && rowA && rowB) glideList(listScrollTarget(listRef.current, rowA, rowB, t, e.clientY));
    const id = items[index].id;
    if (id !== hoverId) setHoverId(id);
  };
  const onTrackLeave = () => setTracking(false);

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
          {sessionId && (
            <div className="shrink-0 px-3 pt-2 pb-1">
              <SegmentedControl
                ariaLabel={t('promptNavigatorTabs')}
                value={shownTab}
                onChange={switchTab}
                options={[
                  { value: 'prompts', label: t('promptNavigatorTabPrompts') },
                  { value: 'topics', label: t('promptNavigatorTabTopics') },
                ]}
              />
            </div>
          )}
          <div ref={listRef} className="list-scroll overflow-y-auto min-h-0">
            {shownTab === 'topics' && sessionId ? (
              // WHY keyed by session: a switch remounts it, which refetches and drops the old chat's ToC.
              <PromptRailTopics
                key={sessionId}
                items={items}
                sessionId={sessionId}
                currentId={currentId}
                hoverId={hoverId}
                onHover={setHoverId}
                onPick={pick}
              />
            ) : items.map(item => (
              <NavigatorRow
                key={item.id}
                item={item}
                isCurrent={item.id === currentId}
                isTarget={item.id === hoverId && item.id !== currentId}
                rowRef={node => { if (node) rowRefs.current.set(item.id, node); else rowRefs.current.delete(item.id); }}
                onHover={() => setHoverId(item.id)}
                onPick={viaKeyboard => pick(item.id, viaKeyboard)}
              />
            ))}
          </div>
        </div>
      )}
      <div
        // WHY presentation: the ticks are a pointer shortcut; the keyboard path is the panel's rows.
        // WHY flush right: a cursor pushed against the screen edge must land on the rail.
        // WHY prompt-rail: rounded left corners by operator decision (see index.css).
        role="presentation"
        className="prompt-rail px-1.5 py-2 bg-surface/40 cursor-pointer pointer-events-auto"
        data-prompt-rail
        onClick={() => { if (!isMouse()) setOpen(v => !v); }}
      >
        <div
          role="presentation"
          className="relative transition-[width] duration-150"
          style={{ height: trackHeight, width: PEAK_WIDTH * scale }}
          onPointerMove={onTrackMove}
          onPointerLeave={onTrackLeave}
        >
          {items.map((item, i) => {
            const isCurrent = item.id === currentId;
            const isTarget = item.id === hoverId && !isCurrent;
            const tickTone = isCurrent ? 'bg-text' : isTarget ? 'bg-text-muted' : 'bg-text-dim';
            // WHY every offset snapped to device pixels: a 3px tick on a fractional y
            // renders blurred, so equal ticks looked unequal.
            const top = snap(edges[i]);
            const height = snap(edges[i + 1]) - top;
            const full = inWindow(i);
            const drawn = full || isCurrent || isTarget || i % stride === 0;
            const thickness = thicknessOf(i, isCurrent);
            return (
              <div
                key={item.id}
                role="presentation"
                data-prompt-tick={item.id}
                aria-current={isCurrent || undefined}
                className={`absolute inset-x-0 ${glide}`}
                style={{ top, height }}
                onClick={e => {
                  if (!isMouse()) return; // touch: the tap toggles the panel instead
                  e.stopPropagation();
                  jump(item.id, false);
                }}
              >
                {drawn && (
                  <div
                    className={`absolute right-0 transition-[width,background-color] duration-150 ${glide} ${tickTone}`}
                    style={{ top: snap(centres[i] - thickness / 2) - top, height: thickness, width: widthOf(i, isCurrent) }}
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
