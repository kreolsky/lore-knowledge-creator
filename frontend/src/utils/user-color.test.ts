import { describe, it, expect } from 'vitest';
import { userColor, PRESENCE_PALETTE } from './user-color';

describe('userColor', () => {
  it('is deterministic — same id maps to same color', () => {
    expect(userColor('user-abc')).toBe(userColor('user-abc'));
  });

  it('returns a value from the fixed palette', () => {
    expect(PRESENCE_PALETTE).toContain(userColor('whatever'));
  });

  it('returns a valid hex color string', () => {
    expect(userColor('user-1')).toMatch(/^#[0-9a-f]{6}$/i);
  });

  it('spreads different ids across the palette (not all one bucket)', () => {
    const ids = Array.from({ length: 60 }, (_, i) => `user-${i}`);
    const distinct = new Set(ids.map(userColor));
    // With 60 ids over a ~10-color palette we expect most buckets used.
    expect(distinct.size).toBeGreaterThan(PRESENCE_PALETTE.length / 2);
  });

  it('handles empty string without throwing', () => {
    expect(() => userColor('')).not.toThrow();
    expect(PRESENCE_PALETTE).toContain(userColor(''));
  });
});
