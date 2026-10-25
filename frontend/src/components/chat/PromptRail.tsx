/**
 * Prompt rail — one tick per user message of the active branch on the message
 * list's right edge, capped at three quarters of the list height: ticks that do not fit at
 * full pitch are compressed into a thinned overview, with a window centred on the
 * hovered tick (at rest, on the current prompt): spacing tapers from full at its
 * centre to the overview's at its edges, tick thickness and length fall as a bell. Plus a navigator panel (prompt + answer preview per row)
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
// Tick geometry in CSS px, before snapping to device pixels.
const TICK_PITCH = 12;
const TICK_THICKNESS = 3;
// The rail never grows past this share of the message list height.
const RAIL_MAX_SHARE = 0.75;
// The rail's vertical padding (py-2), top and bottom each.
const RAIL_PADDING = 8;
// Lens window ticks either side of its centre, at most.
const LENS_RADIUS = 5;
// The focus tick's thickness and length in CSS px; the bell falls from them to the overview's.
const PEAK_THICKNESS = 5;
const PEAK_WIDTH = 24;
// Length of an overview tick, and the least the current tick is drawn at.
const BASE_WIDTH = 12;
const CURRENT_WIDTH = 20;
// With the navigator closed every tick, and the track, is drawn at this share of its length.
const REST_SCALE = 0.5;
// The lens window takes at most this share of the rail; the rest is the compressed overview.
const LENS_MAX_SHARE = 0.6;
// Minimum gap in CSS px between ticks drawn in the compressed overview.
const OVERVIEW_MIN_GAP = 4;
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

/**
 * Pitch and thickness as whole device pixels, with an even gap around the tick.
 * WHY: at a fractional zoom a 3px tick spans 3.3 device px and each one snaps to
 * 3 or 4 on its own; whole-pixel sizes with a shared fractional offset snap alike.
 */
function tickGeometry(dpr: number): { pitch: number; thickness: number } {
  const pitch = Math.round(TICK_PITCH * dpr);
  let thickness = Math.max(1, Math.round(TICK_THICKNESS * dpr));
  if ((pitch - thickness) % 2 !== 0) thickness += 1;
  return { pitch: pitch / dpr, thickness: thickness / dpr };
}
export interface Lens {
  size: number;      // ticks in the window, 2 * radius + 1
  compressed: number; // pitch of every tick outside it
  stride: number;    // every stride-th tick outside it is drawn
}

// Thickness of an overview tick, the layout's reading of the one-device-pixel hairline.
const HAIRLINE = 1;

/** Bell over the window: 1 at its centre, e^-3 at the radius, 0 past it. */
export function lensBell(d: number, radius: number): number {
  if (d > radius) return 0;
  return radius === 0 ? 1 : Math.exp(-3 * (d / radius) ** 2);
}

/** Layout thickness of a tick `d` slots from the window centre; past the radius, a hairline. */
function slotThickness(d: number, radius: number): number {
  return HAIRLINE + (PEAK_THICKNESS - HAIRLINE) * lensBell(d, radius);
}

/**
 * Centre-to-centre distance from tick a to tick a + 1 (either may lie past the rail's
 * ends: a seam with the window counts even there, so the total never depends on start).
 * WHY clear gap, not pitch: the window's ticks are thicker than the overview's
 * hairlines, so equal centre distances read as a tighter window edge. The clear gap
 * between neighbours falls linearly from the full pitch's at the centre to exactly the
 * drawn overview's (stride * compressed - HAIRLINE) at the seam.
 */
function pairDistance(a: number, pitch: number, lens: Lens, start: number): number {
  const { size, compressed, stride } = lens;
  const radius = (size - 1) / 2;
  const centre = start + radius;
  const gap = Math.max(Math.abs(a - centre), Math.abs(a + 1 - centre));
  if (gap > radius + 1) return compressed;
  const share = radius === 0 ? 0 : (radius + 1 - gap) / radius;
  const clear = (stride * compressed - HAIRLINE) * (1 - share) + (pitch - TICK_THICKNESS) * share;
  return clear + (slotThickness(Math.abs(a - centre), radius) + slotThickness(Math.abs(a + 1 - centre), radius)) / 2;
}

/** The lens for `count` ticks that do not fit `height` at full `pitch`. */
export function lensFor(count: number, pitch: number, height: number): Lens {
  let radius = LENS_RADIUS;
  while (radius > 0 && (2 * radius + 2 >= count || pitch * (2 * radius + 1) > height * LENS_MAX_SHARE)) radius--;
  const size = 2 * radius + 1;
  // Rail height (see windowLayout) is linear in compressed: solve it from two probes.
  const railHeight = (lens: Lens) => {
    let total = -lens.compressed;
    for (let a = -1; a < count; a++) total += pairDistance(a, pitch, lens, 0);
    return total;
  };
  const solve = (stride: number): Lens => {
    const h0 = railHeight({ size, compressed: 0, stride });
    const h1 = railHeight({ size, compressed: 1, stride });
    return { size, compressed: (height - h0) / (h1 - h0), stride };
  };
  // The stride is the smallest whose drawn gap clears OVERVIEW_MIN_GAP.
  let lens = solve(1);
  while (lens.stride < count && lens.stride * lens.compressed < OVERVIEW_MIN_GAP) lens = solve(lens.stride + 1);
  return lens;
}

/**
 * Tick centres, hit spans and layout thickness with the window over ticks
 * start..start+size-1: edges[i]..edges[i+1] is tick i's span, edges[0] = 0,
 * edges[count] = height.
 * WHY a stepped window, not a smooth lens: a tick outside the window sits at the same
 * y whatever the window's start, so moving the window moves only the ticks it gains
 * or drops — a smooth lens re-snapped every tick per pixel of pointer travel and shimmered.
 */
export function windowLayout(
  count: number, pitch: number, lens: Lens, start: number,
): { centres: number[]; edges: number[]; thickness: number[] } {
  const centres = [lens.compressed / 2 + pairDistance(-1, pitch, lens, start) - lens.compressed];
  for (let a = 0; a < count - 1; a++) centres.push(centres[a] + pairDistance(a, pitch, lens, start));
  const height = centres[count - 1] + pairDistance(count - 1, pitch, lens, start) - lens.compressed / 2;
  const edges = [0, ...centres.slice(1).map((c, i) => (centres[i] + c) / 2), height];
  const radius = (lens.size - 1) / 2;
  const thickness = centres.map((_, i) => slotThickness(Math.abs(i - start - radius), radius));
  return { centres, edges, thickness };
}

export function windowEdges(count: number, pitch: number, lens: Lens, start: number): number[] {
  return windowLayout(count, pitch, lens, start).edges;
}

/** The tick whose span holds y. */
export function tickAt(edges: number[], y: number): number {
  let i = 0;
  while (i < edges.length - 2 && edges[i + 1] <= y) i++;
  return i;
}

/** The window start that centres `index`, clamped to the rail. */
export function centredStart(count: number, lens: Lens, index: number): number {
  return Math.min(count - lens.size, Math.max(0, index - Math.floor(lens.size / 2)));
}

/**
 * The tick nearest `prev` whose centred window puts y over that same tick.
 * WHY a fixed point, not tickAt on the current layout: re-centring moves the ticks,
 * so the tick under the pointer must be the one its own window draws there. Adjacent
 * ticks' spans overlap (pitch > compressed), which keeps `prev` while it still holds y
 * — that overlap is the hysteresis that stops the window flickering between two ticks.
 */
export function followWindow(
  count: number, pitch: number, lens: Lens, prev: number, y: number,
): { start: number; index: number } {
  for (let shift = 0; shift < count; shift++) {
    for (const i of shift === 0 ? [prev] : [prev - shift, prev + shift]) {
      if (i < 0 || i >= count) continue;
      const start = centredStart(count, lens, i);
      if (tickAt(windowEdges(count, pitch, lens, start), y) === i) return { start, index: i };
    }
  }
  const start = centredStart(count, lens, prev);
  return { start, index: tickAt(windowEdges(count, pitch, lens, start), y) };
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

/** The pair of tick centres around y and y's share of the way between them, clamped to the ends. */
export function segmentAt(centres: number[], y: number): { a: number; t: number } {
  let a = 0;
  while (a < centres.length - 2 && centres[a + 1] <= y) a++;
  const t = (y - centres[a]) / (centres[a + 1] - centres[a]);
  return { a, t: Math.min(1, Math.max(0, t)) };
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
  }, [open, currentId]);

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
    ? windowLayout(count, tick.pitch, lens, windowStart)
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
  const bellAt = (i: number) => lensBell(Math.abs(i - focus), lens ? (lens.size - 1) / 2 : LENS_RADIUS);
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
  const hoverOn = (id: string) => (e: PointerEvent) => {
    if (e.pointerType === 'mouse') setHoverId(id);
  };
  // WHY only from the track: scrolling the panel under a hovered row would slide rows under the pointer.
  const onTrackMove = (e: PointerEvent<HTMLDivElement>) => {
    if (e.pointerType !== 'mouse') return;
    const y = Math.min(trackHeight, Math.max(0, e.clientY - e.currentTarget.getBoundingClientRect().top));
    const followed = lens ? followWindow(count, tick.pitch, lens, focus, y) : null;
    const index = followed ? followed.index : tickAt(edges, y);
    setTracking(true);
    // The centres of the layout this move draws: the followed window's, not the one on screen.
    const drawn = lens && followed ? windowLayout(count, tick.pitch, lens, followed.start).centres : centres;
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
          <div ref={listRef} className="list-scroll overflow-y-auto min-h-0">
            {items.map(item => {
              const preview = answerPreview(item.answer);
              const isCurrent = item.id === currentId;
              const isTarget = item.id === hoverId && !isCurrent;
              const rowTone = isCurrent ? 'bg-surface3' : isTarget ? 'bg-surface2' : '';
              return (
                <div
                  key={item.id}
                  ref={node => { if (node) rowRefs.current.set(item.id, node); else rowRefs.current.delete(item.id); }}
                  role="button"
                  tabIndex={0}
                  aria-current={isCurrent || undefined}
                  data-prompt-row={item.id}
                  onPointerEnter={hoverOn(item.id)}
                  onClick={() => jump(item.id, !isMouse())}
                  onKeyDown={e => { if (e.key === 'Enter') jump(item.id, false); }}
                  className={`px-3 py-2 cursor-pointer transition-colors duration-150 ${rowTone}`}
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
