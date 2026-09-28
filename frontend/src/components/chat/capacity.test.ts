/** Unit tests for computeCapacity — pure height-based page-size math (ResizeObserver-free). */

import { describe, it, expect } from 'vitest';
import { computeCapacity } from './capacity';

describe('computeCapacity', () => {
  it('returns 0 when there are no items', () => {
    expect(computeCapacity(500, 8, 12, 40, 0)).toBe(0);
  });

  it('fits exactly when the usable height is a whole multiple of rowHeight', () => {
    // usable = 500 - 8 - 12 = 480; rowHeight 40 → 12 exactly.
    expect(computeCapacity(500, 8, 12, 40, 20)).toBe(12);
  });

  it('floors a partial leftover row (no rounding up)', () => {
    // usable = 480; rowHeight 45 → 10.666 → 10.
    expect(computeCapacity(500, 8, 12, 45, 20)).toBe(10);
  });

  it('caps at itemCount when the available height overflows', () => {
    // usable = 480; rowHeight 40 → 12, but only 5 items exist.
    expect(computeCapacity(500, 8, 12, 40, 5)).toBe(5);
  });

  it('clamps to a minimum of 1 when the height cannot fit a single row', () => {
    // usable = 480; rowHeight 1000 → 0.48 → 0 → clamped to 1.
    expect(computeCapacity(500, 8, 12, 1000, 10)).toBe(1);
  });

  it('clamps to 1 even when container height is smaller than the padding', () => {
    // usable = 10 - 8 - 12 = negative → floor negative → clamped to 1.
    expect(computeCapacity(10, 8, 12, 40, 10)).toBe(1);
  });

  it('treats a non-positive rowHeight as a single-row capacity', () => {
    // rowHeight 0 would divide-by-zero; must not throw or return Infinity.
    expect(computeCapacity(500, 8, 12, 0, 10)).toBe(1);
    expect(computeCapacity(500, 8, 12, -5, 10)).toBe(1);
  });
});
