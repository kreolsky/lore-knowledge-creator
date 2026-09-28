import { describe, it, expect } from 'vitest';
import { splitContextIds } from './session-helpers';

describe('splitContextIds', () => {
  it('all_docs', () => {
    expect(splitContextIds(['d1', 'd2'], new Set())).toEqual({ docIds: ['d1', 'd2'], refIds: [] });
  });

  it('all_refs', () => {
    expect(splitContextIds(['r1', 'r2'], new Set(['r1', 'r2']))).toEqual({ docIds: [], refIds: ['r1', 'r2'] });
  });

  it('mixed_preserves_order', () => {
    expect(splitContextIds(['d1', 'r1', 'd2', 'r2'], new Set(['r1', 'r2']))).toEqual({
      docIds: ['d1', 'd2'],
      refIds: ['r1', 'r2'],
    });
  });

  it('empty_input', () => {
    expect(splitContextIds([], new Set(['r1']))).toEqual({ docIds: [], refIds: [] });
  });

  it('empty_ref_set_makes_all_docs', () => {
    expect(splitContextIds(['x', 'y'], new Set())).toEqual({ docIds: ['x', 'y'], refIds: [] });
  });
});
