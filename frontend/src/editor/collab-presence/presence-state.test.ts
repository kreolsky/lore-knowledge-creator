import { describe, it, expect } from 'vitest';
import { buildPresenceMap, extractRemoteCursors, type RemoteUser } from './presence-state';

const u = (id: string): RemoteUser => ({ id, name: id, color: '#000000' });

describe('buildPresenceMap', () => {
  // lineNumberAt: every 10 offsets is a new 1-based line.
  const lineAt = (offset: number) => Math.floor(offset / 10) + 1;

  it('maps a single-line caret to its line', () => {
    const map = buildPresenceMap([{ user: u('a'), from: 5, to: 5 }], lineAt);
    expect(map.get(1)).toEqual([u('a')]);
    expect(map.size).toBe(1);
  });

  it('spans every line of a multi-line selection', () => {
    const map = buildPresenceMap([{ user: u('a'), from: 5, to: 25 }], lineAt);
    expect([...map.keys()].sort((x, y) => x - y)).toEqual([1, 2, 3]);
  });

  it('places two users on the same line', () => {
    const map = buildPresenceMap(
      [{ user: u('a'), from: 2, to: 2 }, { user: u('b'), from: 7, to: 7 }],
      lineAt,
    );
    expect(map.get(1)).toEqual([u('a'), u('b')]);
  });

  it('normalises reversed selections (to < from)', () => {
    const map = buildPresenceMap([{ user: u('a'), from: 25, to: 5 }], lineAt);
    expect([...map.keys()].sort((x, y) => x - y)).toEqual([1, 2, 3]);
  });
});

describe('extractRemoteCursors', () => {
  const resolve = (rel: unknown) => (typeof rel === 'number' ? rel : null);

  const states = new Map<number, Record<string, unknown>>([
    [1, { user: u('me'), cursor: { anchor: 0, head: 0 } }],
    [2, { user: u('peer'), cursor: { anchor: 3, head: 8 } }],
    [3, { user: u('no-cursor') }],
  ]);

  it('excludes the local client', () => {
    const cursors = extractRemoteCursors(states, 1, resolve);
    expect(cursors.map(c => c.user.id)).toEqual(['peer']);
  });

  it('skips states without a resolvable cursor', () => {
    const cursors = extractRemoteCursors(states, 99, resolve);
    expect(cursors.map(c => c.user.id).sort()).toEqual(['me', 'peer']);
    const peer = cursors.find(c => c.user.id === 'peer')!;
    expect(peer.from).toBe(3);
    expect(peer.to).toBe(8);
  });

  it('drops cursors whose relative position no longer resolves', () => {
    const stale = new Map<number, Record<string, unknown>>([
      [2, { user: u('peer'), cursor: { anchor: 'gone', head: 'gone' } }],
    ]);
    expect(extractRemoteCursors(stale, 1, resolve)).toEqual([]);
  });
});
