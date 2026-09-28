import { describe, it, expect } from 'vitest';
import { setsEqual, mapsEqual } from './set-equality';

describe('setsEqual', () => {
  it('returns true for two empty sets', () => {
    expect(setsEqual(new Set(), new Set())).toBe(true);
  });

  it('returns true for same content regardless of insertion order', () => {
    expect(setsEqual(new Set(['a', 'b', 'c']), new Set(['c', 'a', 'b']))).toBe(true);
  });

  it('returns false when sizes differ', () => {
    expect(setsEqual(new Set(['a']), new Set(['a', 'b']))).toBe(false);
  });

  it('returns false when one element differs', () => {
    expect(setsEqual(new Set(['a', 'b']), new Set(['a', 'c']))).toBe(false);
  });
});

describe('mapsEqual', () => {
  it('returns true for two empty maps', () => {
    expect(mapsEqual(new Map(), new Map())).toBe(true);
  });

  it('returns true for same entries regardless of insertion order', () => {
    expect(mapsEqual(
      new Map([['a', '1'], ['b', '2']]),
      new Map([['b', '2'], ['a', '1']]),
    )).toBe(true);
  });

  it('returns false when sizes differ', () => {
    expect(mapsEqual(new Map([['a', '1']]), new Map([['a', '1'], ['b', '2']]))).toBe(false);
  });

  it('returns false when same key has different value', () => {
    expect(mapsEqual(new Map([['a', '1']]), new Map([['a', '2']]))).toBe(false);
  });

  it('returns false when keys differ', () => {
    expect(mapsEqual(new Map([['a', '1']]), new Map([['b', '1']]))).toBe(false);
  });
});
