/**
 * Fuzz test for the v2 table model under concurrent replication — the contract the
 * id-keyed schema exists for (see the model header ARCH).
 *
 * Three Yjs replicas drive the model API (add/remove/move row+column, cell writes) with
 * updates exchanged in RANDOM order through deferred mailboxes, so ops routinely run on
 * replicas that have not seen each other's structural changes — the exact races v1's
 * positional arrays merged ragged. Asserts after every full sync:
 *
 *  - replicas equal — `serializeTables` (matrix + widths + display order) is identical;
 *  - no ragged read — every matrix row's length equals the live column count, and the
 *    widths projection matches it (the concurrent add-row × add-column intersection
 *    reads as an empty cell, never as a missing slot);
 *  - no duplicate ids — `(pos, id)` pairs are strictly increasing on both axes (an id
 *    never repeats; equal pos values are legal but still totally ordered by id);
 *  - orphan cells (a remove × add race leaves a cell whose row or column is gone) are
 *    ignored by every read — the matrix stays rectangular.
 *
 * Fixed seeds → a failure is reproducible by re-running the same seed.
 */
// TEST-WHY: contract — the v2 CRDT shape must converge to one rectangular table under
// arbitrary interleavings; a regression here is silent data corruption in collab.
import { describe, it, expect } from 'vitest';
import * as Y from 'yjs';
import {
  createTable,
  readTableModel,
  orderedRowIds,
  orderedColIds,
  addRow,
  addColumn,
  removeRow,
  removeColumn,
  moveRow,
  moveColumn,
  ensureCell,
  TABLE_LOCAL_ORIGIN,
  cellKey,
} from './table-block-model';
import { serializeTables, applyTablesJson } from './table-serialize';
import { DEFAULT_COLUMN_WIDTH } from './table-widths';

/** Deterministic PRNG (mulberry32) — the same seed replays the same scenario. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const REPLICA_COUNT = 3;
const TABLE_ID = 'fuzz-table';

class FuzzWorld {
  docs: Y.Doc[] = [];
  /** Pending INCREMENTAL updates per replica, not yet delivered (the real wire shape). */
  outbox: Uint8Array[][] = [];

  constructor() {
    for (let i = 0; i < REPLICA_COUNT; i++) {
      const doc = new Y.Doc();
      // Delta stream: every local update (ops + remote merges re-broadcast nothing —
      // applyUpdate carries 'remote', so only LOCAL model writes land here).
      doc.on('update', (update: Uint8Array, origin: unknown) => {
        if (origin === 'remote') return;
        this.outbox[i].push(update);
      });
      this.docs.push(doc);
      this.outbox.push([]);
    }
    // The table is born on replica 0 (the sanctioned seeding path, under the fixed
    // TABLE_ID every op addresses) and spread with the first sync.
    applyTablesJson(this.docs[0], JSON.stringify({
      [TABLE_ID]: { columns: [200, 200], rows: [['h0', 'h1'], ['a', 'b']] },
    }));
    this.syncAll();
  }

  /** Deliver every pending update to every replica, in a caller-chosen order. */
  syncAll(rand?: () => number): void {
    const pending = this.outbox.flatMap((box, i) => box.map((u) => [i, u] as const));
    this.outbox = this.docs.map(() => []);
    if (rand) pending.sort(() => rand() - 0.5); // random delivery order
    for (const [from, update] of pending) {
      for (let j = 0; j < REPLICA_COUNT; j++) {
        if (j !== from) Y.applyUpdate(this.docs[j], update, 'remote');
      }
    }
  }

  /** One random mutation on a random replica; returns the replica index. */
  step(rand: () => number): number {
    const i = Math.floor(rand() * REPLICA_COUNT);
    const doc = this.docs[i];
    const rowIds = orderedRowIds(doc, TABLE_ID) ?? [];
    const colIds = orderedColIds(doc, TABLE_ID) ?? [];
    const pick = <T>(xs: T[]): T | null => (xs.length ? xs[Math.floor(rand() * xs.length)] : null);
    const op = Math.floor(rand() * 7);
    switch (op) {
      case 0:
        addRow(doc, TABLE_ID, pick(rowIds), rand() < 0.5 ? null : pick(rowIds));
        break;
      case 1:
        addColumn(doc, TABLE_ID, DEFAULT_COLUMN_WIDTH, pick(colIds), rand() < 0.5 ? null : pick(colIds));
        break;
      case 2:
        if (rowIds.length > 1) removeRow(doc, TABLE_ID, pick(rowIds)!);
        break;
      case 3:
        if (colIds.length > 1) removeColumn(doc, TABLE_ID, pick(colIds)!);
        break;
      case 4:
        if (rowIds.length > 1) moveRow(doc, TABLE_ID, pick(rowIds)!, pick(rowIds), rand() < 0.5 ? null : pick(rowIds));
        break;
      case 5:
        if (colIds.length > 1) moveColumn(doc, TABLE_ID, pick(colIds)!, pick(colIds), rand() < 0.5 ? null : pick(colIds));
        break;
      default: {
        const rid = pick(rowIds);
        const cid = pick(colIds);
        if (rid && cid) write(doc, TABLE_ID, rid, cid, `c${Math.floor(rand() * 1000)}`);
        break;
      }
    }
    return i;
  }
}

/** Structural invariants of ONE replica's table — the "no ragged read" contract. */
function assertTableInvariants(doc: Y.Doc): void {
  const model = readTableModel(doc, TABLE_ID);
  expect(model).not.toBeNull();
  const table = doc.getMap('tables').get(TABLE_ID) as Y.Map<unknown>;
  const colIds = orderedColIds(doc, TABLE_ID)!;
  const rowIds = orderedRowIds(doc, TABLE_ID)!;
  const columns = table.get('columns') as Y.Map<Y.Map<string | number>>;
  const cells = table.get('cells') as Y.Map<Y.Text>;
  // Rectangular: every row has exactly the live column count; widths match the axis.
  expect(model!.rows.every((row) => row.length === colIds.length)).toBe(true);
  expect(model!.columns.length).toBe(colIds.length);
  // No duplicate ids: (pos, id) strictly increasing on both axes — a total order.
  for (const axis of ['rows', 'columns'] as const) {
    const map = table.get(axis) as Y.Map<Y.Map<string>>;
    const pairs = Array.from(map.entries()).map(([id, m]) => `${m.get('pos')}\u0000${id}`).sort();
    expect(new Set(pairs).size).toBe(pairs.length);
  }
  // Orphan cells are ignored by the read: the matrix equals the LIVE row × column
  // intersections only — a cell whose row or column is gone never projects anywhere.
  const expected = rowIds.map((rid) => colIds.map((cid) => cells.get(cellKey(rid, cid))?.toString() ?? ''));
  expect(model!.rows).toEqual(expected);
  expect(model!.columns).toEqual(colIds.map((cid) => Number(columns.get(cid)!.get('w'))));
}

// TEST-WHY: cross-process-contract — CRDT convergence across replicas exchanging
// updates in random order (no infra seams: three in-memory Y.Docs); a regression is
// silent table corruption for collab users.

/** Type into a cell the way the mount path does: ensureCell, then edit its Y.Text. */
function write(doc: Y.Doc, id: string, rowId: string, colId: string, text: string): void {
  const body = ensureCell(doc, id, rowId as never, colId as never);
  if (!body) return;
  doc.transact(() => { body.delete(0, body.length); body.insert(0, text); }, TABLE_LOCAL_ORIGIN);
}

describe('table model v2 fuzz — 3 replicas, random interleaving', () => {
  // TEST-WHY: pure-logic-edge — seeded in-memory PRNG over pure model calls; no seams, deterministic per seed.
  it.each([1, 2, 3, 4, 5])('converges under seed %i (250 ops, sync every 25)', (seed) => {
    const rand = mulberry32(seed);
    const world = new FuzzWorld();
    for (let step = 0; step < 250; step++) {
      world.step(rand);
      // Sometimes run a second op on ANOTHER replica before any delivery — a true race.
      if (rand() < 0.3) world.step(rand);
      if ((step + 1) % 25 === 0) {
        world.syncAll(rand);
        const reference = serializeTables(world.docs[0]);
        for (let j = 1; j < REPLICA_COUNT; j++) {
          expect(serializeTables(world.docs[j])).toBe(reference);
          assertTableInvariants(world.docs[j]);
        }
        assertTableInvariants(world.docs[0]);
      }
    }
    world.syncAll(rand);
    const reference = serializeTables(world.docs[0]);
    for (let j = 1; j < REPLICA_COUNT; j++) {
      expect(serializeTables(world.docs[j])).toBe(reference);
    }
    // WHY 30s, not vitest's 5s: a seed is ~0.3s alone but CPU-bound, and inside the full
    // parallel suite on the CI runner one seed reached 5.1s — a timeout there is a
    // scheduler verdict, not a convergence failure.
  }, 30_000);

  // TEST-WHY: pure-logic-edge — the classic v1 failure (concurrent add/remove on stale replicas) as a direct deterministic scenario.
  it('concurrent add-row / add-column / remove-column merges rectangular', () => {
    const docA = new Y.Doc();
    const id = createTable(docA, [['h0', 'h1'], ['a', 'b']]);
    const uBase = Y.encodeStateAsUpdate(docA);
    const docB = new Y.Doc();
    const docC = new Y.Doc();
    Y.applyUpdate(docB, uBase, 'remote');
    Y.applyUpdate(docC, uBase, 'remote');

    const colsB = orderedColIds(docB, id)!;
    const rowC = orderedRowIds(docC, id)!;

    addRow(docA, id); // A adds a row (stale: no B/C ops yet)
    const rowFromA = orderedRowIds(docA, id)!.find((rid) => !rowC.includes(rid))!; // captured BEFORE the merge
    addColumn(docB, id, DEFAULT_COLUMN_WIDTH, colsB.at(-1)!, null); // B appends a column
    removeColumn(docC, id, colsB[0]); // C removes the first column
    write(docC, id, rowC[0], colsB[1], 'kept'); // C edits the surviving column

    const updates = [Y.encodeStateAsUpdate(docA), Y.encodeStateAsUpdate(docB), Y.encodeStateAsUpdate(docC)];
    for (const doc of [docA, docB, docC]) {
      for (const u of updates) Y.applyUpdate(doc, u, 'remote');
      const model = readTableModel(doc, id)!;
      expect(model.columns).toHaveLength(2); // 2 + 1 - 1
      expect(model.rows).toHaveLength(3); // 2 + 1
      expect(model.rows.every((r) => r.length === 2)).toBe(true);
      expect(model.rows[0][0]).toBe('kept'); // C's edit survived at the right cell
    }
    // The eager-cells rule and its one legal exception: B's column carried a cell for
    // every row LIVE ON B; A's concurrent row was not live there, so that ONE cell is
    // missing (the intersection) and reads EMPTY — never a ragged slot.
    const merged = readTableModel(docA, id)!;
    const colIds = orderedColIds(docA, id)!;
    const rowIds = orderedRowIds(docA, id)!;
    const cells = (docA.getMap('tables').get(id) as Y.Map<unknown>).get('cells') as Y.Map<Y.Text>;
    const newCol = colIds.find((cid) => !colsB.includes(cid))!;
    expect(cells.has(cellKey(rowFromA, newCol))).toBe(false);
    expect(merged.rows[rowIds.indexOf(rowFromA)][colIds.indexOf(newCol)]).toBe('');
  });
});
