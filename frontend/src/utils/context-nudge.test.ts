/** The nudge threshold rule: expected values are
 * the worked table — the operator's contract, not a run of
 * the function. Floor 150k, percents 20/25/30 unless the row says otherwise. */
import { describe, it, expect } from 'vitest';
import { contextNudgeThresholds, contextNudgeCrossed } from './context-nudge';

const CASES: [string, number, number, number[], number[]][] = [
  ['1M window: every stop clears the floor', 1_000_000, 150_000, [20, 25, 30], [200_000, 250_000, 300_000]],
  ['512k window: 20% and 25% fall under the floor and are dropped, 30% stays', 524_288, 150_000, [20, 25, 30], [157_286]],
  ['500k window: a stop exactly on the floor is kept', 500_000, 150_000, [20, 25, 30], [150_000]],
  ['128k window: every stop under the floor — never nudged, nothing lifted', 128_000, 150_000, [20, 25, 30], []],
  ['the drive dial: floor 2000 leaves the percentages alone', 128_000, 2_000, [10, 20, 30], [12_800, 25_600, 38_400]],
  ['no known window: never nudged', 0, 150_000, [20, 40, 50], []],
];

describe('contextNudgeThresholds', () => {
  // TEST-WHY: pure-logic-edge — the clamp/floor/dedupe rule decides when a person is told to start a new chat; the floor DROPS a stop under it and never lifts one (operator's rule) — the 512k and 128k rows are the cases a clamp would get wrong.
  it.each(CASES)('%s', (_name, contextWindow, floor, percents, expected) => {
    expect(contextNudgeThresholds(contextWindow, floor, percents)).toEqual(expected);
  });
});

describe('contextNudgeCrossed', () => {
  // TEST-WHY: pure-logic-edge — the boundary `t <= used` decides whether the banner is due right at a stop; an off-by-one here nudges one turn late at every threshold.
  it('counts a stop the chat sits exactly on', () => {
    const thresholds = contextNudgeThresholds(1_000_000, 150_000, [20, 40, 50]);
    expect(contextNudgeCrossed(thresholds, 0)).toBe(0);
    expect(contextNudgeCrossed(thresholds, 199_999)).toBe(0);
    expect(contextNudgeCrossed(thresholds, 200_000)).toBe(1);
    expect(contextNudgeCrossed(thresholds, 450_000)).toBe(2);
    expect(contextNudgeCrossed(thresholds, 5_000_000)).toBe(3);
  });
});
