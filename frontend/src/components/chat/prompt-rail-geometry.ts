/**
 * Prompt rail tick geometry — pure layout math, no DOM: device-pixel snapping, the lens
 * (a full-pitch window over a compressed overview), tick spans and the pointer-follow
 * fixed point. Consumed by PromptRail only.
 */

// Tick geometry in CSS px, before snapping to device pixels.
const TICK_PITCH = 12;
const TICK_THICKNESS = 3;
// Lens window ticks either side of its centre, at most.
export const LENS_RADIUS = 5;
// The focus tick's thickness in CSS px; the bell falls from it to the overview's.
export const PEAK_THICKNESS = 5;
// The lens window takes at most this share of the rail; the rest is the compressed overview.
const LENS_MAX_SHARE = 0.6;
// Minimum gap in CSS px between ticks drawn in the compressed overview.
const OVERVIEW_MIN_GAP = 4;

/**
 * Pitch and thickness as whole device pixels, with an even gap around the tick.
 * WHY: at a fractional zoom a 3px tick spans 3.3 device px and each one snaps to
 * 3 or 4 on its own; whole-pixel sizes with a shared fractional offset snap alike.
 */
export function tickGeometry(dpr: number): { pitch: number; thickness: number } {
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
export const HAIRLINE = 1;

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

/** Window offset of tick x from `focus` in radius units, stretched per side to the window's edge. */
function focusOffset(x: number, lens: Lens, start: number, focus: number): number {
  const radius = (lens.size - 1) / 2;
  if (x === focus) return 0;
  const reach = x < focus ? focus - start : start + lens.size - 1 - focus;
  return reach === 0 ? radius + 2 : Math.abs(x - focus) * radius / reach;
}

/**
 * The window's pair distances (a = start-1 .. start+size-1) with the hump on `focus`.
 * WHY rescaled to the centred sum: ticks outside the window must keep their y whatever
 * the focus, so the window's total height may not change — only how it is shared.
 */
function focusedDistances(count: number, pitch: number, lens: Lens, start: number, focus: number): Map<number, number> {
  const { compressed, stride } = lens;
  const radius = (lens.size - 1) / 2;
  const raw = new Map<number, number>();
  let centred = 0;
  let focused = 0;
  for (let a = start - 1; a < start + lens.size && a < count; a++) {
    const d0 = focusOffset(a, lens, start, focus);
    const d1 = focusOffset(a + 1, lens, start, focus);
    const gap = Math.max(d0, d1);
    const share = gap > radius + 1 || radius === 0 ? 0 : Math.min(1, (radius + 1 - gap) / radius);
    const clear = (stride * compressed - HAIRLINE) * (1 - share) + (pitch - TICK_THICKNESS) * share;
    const d = clear + (slotThickness(d0, radius) + slotThickness(d1, radius)) / 2;
    raw.set(a, d);
    focused += d;
    centred += pairDistance(a, pitch, lens, start);
  }
  const k = focused > 0 ? centred / focused : 1;
  raw.forEach((d, a) => raw.set(a, d * k));
  return raw;
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
  count: number, pitch: number, lens: Lens, start: number, focus = start + (lens.size - 1) / 2,
): { centres: number[]; edges: number[]; thickness: number[] } {
  const dist = focusedDistances(count, pitch, lens, start, focus);
  const at = (a: number) => dist.get(a) ?? pairDistance(a, pitch, lens, start);
  const centres = [lens.compressed / 2 + at(-1) - lens.compressed];
  for (let a = 0; a < count - 1; a++) centres.push(centres[a] + at(a));
  const height = centres[count - 1] + at(count - 1) - lens.compressed / 2;
  const edges = [0, ...centres.slice(1).map((c, i) => (centres[i] + c) / 2), height];
  const radius = (lens.size - 1) / 2;
  const thickness = centres.map((_, i) => slotThickness(focusOffset(i, lens, start, focus), radius));
  return { centres, edges, thickness };
}

export function windowEdges(count: number, pitch: number, lens: Lens, start: number, focus?: number): number[] {
  return windowLayout(count, pitch, lens, start, focus).edges;
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
      if (tickAt(windowEdges(count, pitch, lens, start, i), y) === i) return { start, index: i };
    }
  }
  const start = centredStart(count, lens, prev);
  return { start, index: tickAt(windowEdges(count, pitch, lens, start, prev), y) };
}

/** The pair of tick centres around y and y's share of the way between them, clamped to the ends. */
export function segmentAt(centres: number[], y: number): { a: number; t: number } {
  let a = 0;
  while (a < centres.length - 2 && centres[a + 1] <= y) a++;
  const t = (y - centres[a]) / (centres[a + 1] - centres[a]);
  return { a, t: Math.min(1, Math.max(0, t)) };
}
