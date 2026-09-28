import { describe, it, expect } from 'vitest';
import { upsertByKey } from './upsert';

type Item = { id: number; v: string };

describe('upsertByKey', () => {
  it('appends when the key is absent', () => {
    expect(upsertByKey<Item>([{ id: 1, v: 'a' }], { id: 2, v: 'b' }, 'id')).toEqual([
      { id: 1, v: 'a' },
      { id: 2, v: 'b' },
    ]);
  });

  it('replaces in place when the key matches (race-safe dedup)', () => {
    const out = upsertByKey<Item>([{ id: 1, v: 'a' }, { id: 2, v: 'b' }], { id: 1, v: 'c' }, 'id');
    expect(out).toEqual([{ id: 1, v: 'c' }, { id: 2, v: 'b' }]);
  });

  it('returns a new array (never mutates the input)', () => {
    const input: Item[] = [{ id: 1, v: 'a' }];
    const out = upsertByKey(input, { id: 1, v: 'x' }, 'id');
    expect(out).not.toBe(input);
    expect(input).toEqual([{ id: 1, v: 'a' }]); // original untouched
  });

  it('deduplicates a realtime+optimistic race to one entry', () => {
    // The actor's optimistic create and the broadcast frame both carry message_id 'm1'.
    const afterOptimistic = upsertByKey(
      [] as { message_id: string; content: string }[],
      { message_id: 'm1', content: 'x' },
      'message_id',
    );
    const afterRealtime = upsertByKey(afterOptimistic, { message_id: 'm1', content: 'x' }, 'message_id');
    expect(afterRealtime).toHaveLength(1);
  });
});
