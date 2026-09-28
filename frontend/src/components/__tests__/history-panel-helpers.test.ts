// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import type { Checkpoint } from '../../types';
import { upsertSnapshotById, appendDedupSnapshots } from '../history-panel-helpers';

const snap = (id: string, over: Partial<Checkpoint> = {}): Checkpoint => ({
  checkpoint_id: id,
  document_id: 'd1',
  content: '',
  label: 'my-snapshot',
  comment: '',
  created_at: '2026-01-01T00:00:00Z',
  ...over,
});

describe('upsertSnapshotById', () => {
  it('prepends a new snapshot by checkpoint_id', () => {
    const prev = [snap('a'), snap('b')];
    const next = upsertSnapshotById(prev, snap('c'));
    expect(next.map(s => s.checkpoint_id)).toEqual(['c', 'a', 'b']);
  });

  it('replaces an existing same-id snapshot in place (upsert, not drop)', () => {
    const prev = [snap('a', { comment: 'old' }), snap('b')];
    const next = upsertSnapshotById(prev, snap('a', { comment: 'new', content: 'refreshed' }));
    expect(next.map(s => s.checkpoint_id)).toEqual(['a', 'b']);
    expect(next[0].comment).toBe('new');
    expect(next[0].content).toBe('refreshed');
  });
});

describe('appendDedupSnapshots', () => {
  it('appends only ids not already present', () => {
    const existing = [snap('a'), snap('b')];
    const batch = [snap('b'), snap('c'), snap('d')]; // 'b' already present
    const next = appendDedupSnapshots(existing, batch);
    expect(next.map(s => s.checkpoint_id)).toEqual(['a', 'b', 'c', 'd']);
  });

  it('returns the same array reference when nothing is new (no-op)', () => {
    const existing = [snap('a'), snap('b')];
    const batch = [snap('a'), snap('b')];
    expect(appendDedupSnapshots(existing, batch)).toBe(existing);
  });
});
