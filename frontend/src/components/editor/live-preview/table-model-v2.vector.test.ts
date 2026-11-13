/**
 * Vector tests over the SHARED fixture `fixtures/table-v2.json`: npm `fractional-indexing`
 * (key `fractional_indexing`) and the legacy-width freeze (key `freeze_auto_widths`).
 *
 * The vectors were written by npm `fractional-indexing@3.2.0` (step 1 of the table-model
 * v2 epic) and replayed by the backend `tests/backend/test_fractional_key_parity.py`
 * against PyPI `0.1.3`. This side asserts the pinned npm package still reproduces its own
 * recorded vectors — a version bump or behavior drift in the pin turns this red BEFORE the
 * two sides mint disagreeing row/column `pos` keys.
 */
import { describe, it, expect } from 'vitest';
import { generateKeyBetween, generateNKeysBetween } from 'fractional-indexing';
import vectors from './fixtures/table-v2.json';
import { freezeAutoWidths } from './table-widths';
import { resolveMergeEntries } from './table-block-model';

// TEST-WHY: cross-process-contract — the vectors are shared with the backend pytest; this side proves the npm pin still reproduces them.
describe('fractional-indexing vector parity (fixtures/table-v2.json)', () => {
  // TEST-WHY: cross-process-contract — the vectors are shared with the backend pytest; this side proves the npm pin still reproduces them.
  it('the npm pin reproduces every recorded vector, in order', () => {
    const data = vectors.fractional_indexing;
    expect(data.generator).toBe('fractional-indexing@3.2.0');
    expect(data.between.length).toBeGreaterThan(200);
    expect(data.n_between.length).toBeGreaterThan(30);
    for (const v of data.between) {
      const key = generateKeyBetween(v.a, v.b);
      expect(key).toBe(v.key);
      // Total order property: a < key < b (a fractional index must sort inside its gap).
      if (v.a !== null) expect(v.a < key).toBe(true);
      if (v.b !== null) expect(key < v.b).toBe(true);
    }
    for (const v of data.n_between) {
      expect(generateNKeysBetween(v.a, v.b, v.n)).toEqual(v.keys);
    }
  });
});

describe('freezeAutoWidths vector parity (fixtures/table-v2.json)', () => {
  // TEST-WHY: cross-process-contract — the backend freeze_auto_widths replays the same vectors; both sides must write equal px into the shared model.
  it('reproduces every recorded vector', () => {
    expect(vectors.freeze_auto_widths.length).toBeGreaterThan(0);
    for (const v of vectors.freeze_auto_widths) {
      expect({ case: v.case, widths: freezeAutoWidths(v.widths, v.matrix) }).toEqual({ case: v.case, widths: v.expected });
    }
  });
});

// TEST-WHY: cross-process-contract — the backend `resolve_merge_entries` replays the same vectors; two resolvers that disagree render a different merged area than the agent and checkpoints see.
describe('resolveMergeEntries vector parity (fixtures/table-v2.json → merges)', () => {
  // TEST-WHY: cross-process-contract — the backend resolver replays the same vectors; disagreeing resolvers show the agent a different area than the editor.
  it('reproduces every recorded vector, in order', () => {
    const got = vectors.merges.map((v) => [
      v.case,
      resolveMergeEntries(v.rows, v.cols, Object.entries(v.merges).map(
        ([k, e]) => [k, new Map(Object.entries(e as Record<string, string>))] as [string, unknown],
      )).map((a) => [a.r0, a.c0, a.r1, a.c1]),
    ]);
    expect(got).toEqual(vectors.merges.map((v) => [v.case, v.expected]));
  });
});
