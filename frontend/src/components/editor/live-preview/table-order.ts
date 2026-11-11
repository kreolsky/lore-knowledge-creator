/** Table axis order — the ONE order function, its per-table cache and fractional `pos` keys (see SYSTEM: table-block, table-block-model.ts). */
import type * as Y from 'yjs';
import { generateKeyBetween } from 'fractional-indexing';
import type { AxisEntry, ColId, RowId, TableMap, TableV2Parts } from './table-block-model';

/**
 * Row or column ids of one axis map in display order: sort by `(pos, id)`, plain
 * string comparison. The ONE order function on this side — every reader (render, the
 * matrix projections, move bounds) goes through it or through the cached wrappers.
 * Backend twin: `table_serialize.ordered_axis_ids`.
 */
export function orderedAxisIds(axis: Y.Map<AxisEntry>): string[] {
  return Array.from(axis.entries())
    .map(([id, entry]) => [String(entry.get('pos')), id] as [string, string])
    .sort(([pa, ia], [pb, ib]) => (pa < pb ? -1 : pa > pb ? 1 : ia < ib ? -1 : 1))
    .map(([, id]) => id);
}

/**
 * Per-table order cache, invalidated ONLY by an observer on that table's `rows`/
 * `columns` maps — never by a cell-text edit (a keystroke must not re-sort anything).
 *
 * ARCH: one `observeDeep` on the TABLE map, filtered by the event path's first key:
 * `rows`/`columns` (add/remove AND a nested `pos` write — a move) drop that axis, a
 * change on the table map itself drops both, `cells` drops nothing. A width write drops
 * the column order too — a wasted re-sort of a few dozen entries, accepted so
 * invalidation needs no per-write-path discipline. Yjs fires observers synchronously at
 * the end of the outermost transaction, so a read after a model write always sees
 * fresh order.
 *
 * INVARIANT: the invalidating observer sits on the TABLE map, never on the axis maps.
 * Why: Yjs runs deep observers in the order their types were FIRST touched in the
 * transaction (`changedParentTypes` is an insertion-ordered Map filled by walking up
 * from each changed type). Every change under a table walks through the table before
 * reaching `tables`, so a `tables`-level reader (the widget, the find index) always
 * runs after this one; an axis-map observer runs after `tables` whenever the update
 * touched `cells` first (a remote update's delete set), and that reader then renders
 * the stale order.
 */
const orderCache = new WeakMap<TableMap, { rowIds: RowId[] | null; colIds: ColId[] | null }>();

export function cachedOrder(table: TableMap, parts: TableV2Parts): { rowIds: RowId[]; colIds: ColId[] } {
  let entry = orderCache.get(table);
  if (!entry) {
    entry = { rowIds: null, colIds: null };
    orderCache.set(table, entry);
    const held = entry;
    table.observeDeep((events) => {
      for (const event of events) {
        const head = event.path[0];
        if (head === 'rows' || head === undefined) held.rowIds = null;
        if (head === 'columns' || head === undefined) held.colIds = null;
      }
    });
  }
  entry.rowIds ??= orderedAxisIds(parts.rows) as RowId[];
  entry.colIds ??= orderedAxisIds(parts.columns) as ColId[];
  return { rowIds: entry.rowIds, colIds: entry.colIds };
}

/**
 * A `pos` key for an item placed right after `beforeId` and right before `afterId`
 * (either may be `null` = that end of the axis). Backend twin: `_pos_between`.
 *
 * WHY: equal `pos` values are legal (two replicas inserting at the same gap mint the
 * same key); `generate_key_between` raises on `a >= b`, so the upper bound is then
 * taken from the next DISTINCT `pos` after the lower one. An anchor id the race already
 * removed degrades to that side's END (`null` bound) — the op still lands somewhere
 * sane instead of throwing.
 */
export function posBetween(axis: Y.Map<AxisEntry>, beforeId: string | null, afterId: string | null): string {
  const lo = beforeId !== null ? String(axis.get(beforeId)?.get('pos') ?? '') : null;
  const hi = afterId !== null ? String(axis.get(afterId)?.get('pos') ?? '') : null;
  const loKey = lo === '' ? null : lo;
  const hiKey = hi === '' ? null : hi;
  if (loKey !== null && (hiKey === null || loKey >= hiKey)) {
    const next = Array.from(axis.values())
      .map((m) => String(m.get('pos')))
      .sort()
      .find((p) => p > loKey);
    return generateKeyBetween(loKey, next ?? null);
  }
  return generateKeyBetween(loKey, hiKey);
}

/** The greatest `pos` on an axis, `null` when empty. */
function lastPos(axis: Y.Map<AxisEntry>): string | null {
  return axis.size ? Array.from(axis.values()).map((m) => String(m.get('pos'))).sort().at(-1)! : null;
}

/** The pos of an item APPENDED to an axis's end — the add APIs' both-anchors-null
 * default (Tab/agent append). NOTE: `generateKeyBetween(null, null)` would mint the
 * FIRST key instead, so the default must route through here, not through `posBetween`. */
export function appendPos(axis: Y.Map<AxisEntry>): string {
  return generateKeyBetween(lastPos(axis), null);
}
