/** Tests for the bounded LRU render memo (utils/bounded-memo.ts). */
import { describe, it, expect } from 'vitest';
import { createBoundedMemo } from './bounded-memo';

describe('createBoundedMemo', () => {
  it('evicts the least-recently-USED entry, not the least-recently-inserted', () => {
    const m = createBoundedMemo<number>(3);
    m.set('a', 1);
    m.set('b', 2);
    m.set('c', 3);
    // Touch 'a' — it must survive the next insert; 'b' (untouched) must go.
    expect(m.get('a')).toBe(1);
    m.set('d', 4);
    expect(m.get('a')).toBe(1);
    expect(m.get('b')).toBeUndefined();
    expect(m.get('c')).toBe(3);
    expect(m.get('d')).toBe(4);
  });

  it('re-setting an existing key updates the value and refreshes recency without growing', () => {
    const m = createBoundedMemo<string>(2);
    m.set('a', '1');
    m.set('b', '2');
    m.set('a', '1-new'); // refresh 'a', still 2 entries
    expect(m.size).toBe(2);
    m.set('c', '3'); // evicts 'b' (LRU), not 'a'
    expect(m.get('a')).toBe('1-new');
    expect(m.get('b')).toBeUndefined();
  });

  it('a get refreshes recency so a hot entry survives an insert burst', () => {
    const m = createBoundedMemo<number>(2);
    m.set('hot', 1);
    m.set('x', 2);
    expect(m.get('hot')).toBe(1);
    m.set('y', 3); // evicts 'x'
    expect(m.get('hot')).toBe(1);
    expect(m.get('x')).toBeUndefined();
  });

  it('clear() drops everything', () => {
    const m = createBoundedMemo<number>(5);
    m.set('a', 1);
    m.clear();
    expect(m.get('a')).toBeUndefined();
    expect(m.size).toBe(0);
  });

  it('holds exactly maxEntries after a insert-only scan', () => {
    const m = createBoundedMemo<number>(3);
    for (let i = 0; i < 10; i++) m.set(`k${i}`, i);
    expect(m.size).toBe(3);
    // The last three inserted survive insertion-order eviction.
    expect(m.get('k9')).toBe(9);
    expect(m.get('k8')).toBe(8);
    expect(m.get('k7')).toBe(7);
    expect(m.get('k0')).toBeUndefined();
  });
});
