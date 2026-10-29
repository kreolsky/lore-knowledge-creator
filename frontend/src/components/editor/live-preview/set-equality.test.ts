import { describe, it, expect } from 'vitest';
import { setsEqual, mapsEqual } from './set-equality';

describe('setsEqual', () => {

  it('returns false when sizes differ', () => {
    expect(setsEqual(new Set(['a']), new Set(['a', 'b']))).toBe(false);
  });
});

describe('mapsEqual', () => {

  it('returns false when sizes differ', () => {
    expect(mapsEqual(new Map([['a', '1']]), new Map([['a', '1'], ['b', '2']]))).toBe(false);
  });
});
