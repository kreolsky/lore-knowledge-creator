/** Pure pivots of the token-usage fact cube (see SYSTEM: token-usage): filters, KPIs, daily series, rankings.
 *
 * The server returns one sparse day × user × model × source cube per range; every
 * filter click, axis flip and metric switch re-slices it here with no round trip.
 * A user in several groups counts in each group (group totals can exceed the total);
 * no user = the SYSTEM key, a user in no group = the NO_GROUP key — real rows, never dropped.
 */

import type { TokenUsageCube, TokenUsageCubeRow } from '../../../api/admin';

export type Metric = 'total' | 'input' | 'output' | 'cache_read' | 'cache_write';
export type Subject = 'user' | 'group';
export type FilterKind = 'user' | 'group' | 'model';
export interface Filter { kind: FilterKind; id: string }
export interface Ranked { key: string; value: number }

export const SYSTEM_KEY = '__system__';
export const NO_GROUP_KEY = '__no_group__';
export const OTHER_KEY = '__other__';
export const SERIES_SLOTS = 6;
export const DAY_MS = 86_400_000;

export function metricOf(r: TokenUsageCubeRow, m: Metric): number {
  return m === 'total' ? r.input + r.output + r.cache_read + r.cache_write : r[m];
}

export function userKey(r: TokenUsageCubeRow): string {
  return r.user_id ?? SYSTEM_KEY;
}

/** user key → the group keys it counts in (CURRENT membership). */
export function membershipOf(cube: TokenUsageCube): (user: string) => string[] {
  const map = new Map<string, string[]>();
  for (const [gid, g] of Object.entries(cube.groups)) {
    for (const uid of g.members) map.set(uid, [...(map.get(uid) ?? []), gid]);
  }
  return user => (user === SYSTEM_KEY ? [SYSTEM_KEY] : map.get(user) ?? [NO_GROUP_KEY]);
}

export type KeysOf = (r: TokenUsageCubeRow) => string[];

export function keysOf(dim: FilterKind, memberOf: (u: string) => string[]): KeysOf {
  const table: Record<FilterKind, KeysOf> = {
    user: r => [userKey(r)],
    group: r => memberOf(userKey(r)),
    model: r => [r.model],
  };
  return table[dim];
}

/** Filters of one kind OR together; different kinds AND. */
export function applyFilters(rows: TokenUsageCubeRow[], filters: Filter[], memberOf: (u: string) => string[]): TokenUsageCubeRow[] {
  if (filters.length === 0) return rows;
  const byKind = new Map<FilterKind, Set<string>>();
  for (const f of filters) byKind.set(f.kind, (byKind.get(f.kind) ?? new Set()).add(f.id));
  const checks = [...byKind].map(([kind, ids]) => {
    const keys = keysOf(kind, memberOf);
    return (r: TokenUsageCubeRow) => keys(r).some(k => ids.has(k));
  });
  return rows.filter(r => checks.every(c => c(r)));
}

export interface Kpis { total: number; input: number; output: number; cacheHit: number | null; activeUsers: number }

export function kpis(rows: TokenUsageCubeRow[]): Kpis {
  let input = 0, output = 0, cacheRead = 0, cacheWrite = 0;
  const users = new Set<string>();
  for (const r of rows) {
    input += r.input; output += r.output; cacheRead += r.cache_read; cacheWrite += r.cache_write;
    if (r.user_id) users.add(r.user_id);
  }
  const prompt = input + cacheRead;
  return {
    total: input + output + cacheRead + cacheWrite,
    input,
    output,
    cacheHit: prompt > 0 ? cacheRead / prompt : null,
    activeUsers: users.size,
  };
}

/** Sum of the metric per key, descending; zero keys dropped. */
export function rank(rows: TokenUsageCubeRow[], keys: KeysOf, metric: Metric): Ranked[] {
  const acc = new Map<string, number>();
  for (const r of rows) {
    const v = metricOf(r, metric);
    for (const k of keys(r)) acc.set(k, (acc.get(k) ?? 0) + v);
  }
  return [...acc].map(([key, value]) => ({ key, value })).filter(x => x.value > 0).sort((a, b) => b.value - a.value);
}

export function totalOf(rows: TokenUsageCubeRow[], metric: Metric): number {
  return rows.reduce((s, r) => s + metricOf(r, metric), 0);
}

export interface DayStack { day: string; total: number; parts: Ranked[] }

/** One stack per day in `days`; keys outside `series` fold into OTHER_KEY. Days without spend have no parts. */
export function dailyStacks(rows: TokenUsageCubeRow[], days: string[], keys: KeysOf, series: string[], metric: Metric): DayStack[] {
  const known = new Set(series);
  const byDay = new Map<string, Map<string, number>>();
  for (const r of rows) {
    const v = metricOf(r, metric);
    if (v === 0) continue;
    const day = byDay.get(r.day) ?? new Map<string, number>();
    for (const k of keys(r)) {
      const key = known.has(k) ? k : OTHER_KEY;
      day.set(key, (day.get(key) ?? 0) + v);
    }
    byDay.set(r.day, day);
  }
  const order = [...series, OTHER_KEY];
  return days.map(day => {
    const m = byDay.get(day);
    const parts = m ? order.filter(k => m.has(k)).map(k => ({ key: k, value: m.get(k)! })) : [];
    return { day, total: parts.reduce((s, p) => s + p.value, 0), parts };
  });
}

export function utcDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

export function dayMs(day: string): number {
  return Date.parse(`${day}T00:00:00Z`);
}

export function rangeDays(from: string, to: string): number {
  return Math.round((dayMs(to) - dayMs(from)) / DAY_MS) + 1;
}

export function daysInRange(from: string, to: string): string[] {
  const n = rangeDays(from, to);
  return Array.from({ length: Math.max(0, n) }, (_, i) => utcDay(dayMs(from) + i * DAY_MS));
}

/** The equal-length range that ends the day before `from`. */
export function previousRange(from: string, to: string): { from: string; to: string } {
  const n = rangeDays(from, to);
  return { from: utcDay(dayMs(from) - n * DAY_MS), to: utcDay(dayMs(from) - DAY_MS) };
}

const compactFmt = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });

export function compact(n: number): string {
  return compactFmt.format(n);
}

/** CSS colour of a series slot; past the slots (and OTHER_KEY) = the neutral "other". */
export function slotColor(index: number): string {
  return index >= 0 && index < SERIES_SLOTS ? `var(--viz-${index + 1})` : 'var(--viz-other)';
}
