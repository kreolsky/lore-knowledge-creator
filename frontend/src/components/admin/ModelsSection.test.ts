import { describe, expect, it } from 'vitest';
import { moveAfter, subjectsForTier, tierOf } from './ModelsSection';
import { HIDDEN_SUBJECT, PUBLIC_SUBJECT, TOOLS_SUBJECT } from '../../api/admin';

const GROUP = 'group:g1';

describe('model tier ladder', () => {
  it('derives each tier from its subjects', () => {
    expect(tierOf([HIDDEN_SUBJECT])).toBe('hidden');
    expect(tierOf([TOOLS_SUBJECT])).toBe('tools');
    expect(tierOf([GROUP])).toBe('private');
    expect(tierOf([GROUP, PUBLIC_SUBJECT])).toBe('public');
  });

  it('Tools drops every grant, like Hidden', () => {
    expect(subjectsForTier([GROUP, PUBLIC_SUBJECT], 'tools')).toEqual([TOOLS_SUBJECT]);
    expect(subjectsForTier([GROUP, PUBLIC_SUBJECT], 'hidden')).toEqual([HIDDEN_SUBJECT]);
  });

  it('leaving Tools never carries the tools marker into a grant list', () => {
    expect(subjectsForTier([TOOLS_SUBJECT], 'private')).toEqual([]);
    expect(subjectsForTier([TOOLS_SUBJECT], 'public')).toEqual([PUBLIC_SUBJECT]);
  });

  it('round-trips every tier', () => {
    for (const tier of ['hidden', 'tools', 'private', 'public'] as const) {
      expect(tierOf(subjectsForTier([GROUP], tier))).toBe(tier);
    }
  });
});

describe('model drag order', () => {
  const rows = ['a', 'b', 'c', 'd'].map(id => ({ id }));
  const ids = (r: { id: string }[]) => r.map(x => x.id);

  it('moves a row to the top, after a sibling, and to the end', () => {
    expect(ids(moveAfter(rows, 'c', null))).toEqual(['c', 'a', 'b', 'd']);
    expect(ids(moveAfter(rows, 'a', 'c'))).toEqual(['b', 'c', 'a', 'd']);
    expect(ids(moveAfter(rows, 'b', 'd'))).toEqual(['a', 'c', 'd', 'b']);
  });

  it('leaves the list untouched for an unknown id', () => {
    expect(moveAfter(rows, 'x', null)).toBe(rows);
  });
});
