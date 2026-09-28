import { describe, it, expect } from 'vitest';
import { addWithCascade, subtractWithCascade, type FirstCircle } from './cascade-selection';

describe('addWithCascade', () => {
  it('adds parent doc and merges its first-circle children', () => {
    const next = addWithCascade(
      { docIds: [], refIds: [] },
      'doc',
      'd1',
      { document_ids: ['d2'], reference_ids: ['r1', 'r2'] },
    );
    expect(next.docIds.sort()).toEqual(['d1', 'd2']);
    expect(next.refIds.sort()).toEqual(['r1', 'r2']);
  });

  it('does not duplicate already-selected ids', () => {
    const next = addWithCascade(
      { docIds: ['d2'], refIds: ['r1'] },
      'doc',
      'd1',
      { document_ids: ['d2'], reference_ids: ['r1', 'r2'] },
    );
    expect(next.docIds.sort()).toEqual(['d1', 'd2']);
    expect(next.refIds.sort()).toEqual(['r1', 'r2']);
  });

  it('adds parent ref and merges children', () => {
    const next = addWithCascade(
      { docIds: [], refIds: [] },
      'ref',
      'r0',
      { document_ids: ['d1'], reference_ids: ['r9'] },
    );
    expect(next.refIds.sort()).toEqual(['r0', 'r9']);
    expect(next.docIds).toEqual(['d1']);
  });
});

describe('subtractWithCascade', () => {
  const empty: FirstCircle = { document_ids: [], reference_ids: [] };

  it('removes the target id and its first-circle children (set difference)', () => {
    const next = subtractWithCascade(
      { docIds: ['d1', 'd2', 'd3'], refIds: ['r1', 'r2'] },
      'doc',
      'd1',
      { document_ids: ['d2'], reference_ids: ['r1'] },
    );
    expect(next.docIds.sort()).toEqual(['d3']);
    expect(next.refIds.sort()).toEqual(['r2']);
  });

  it('removes the target id even with empty first-circle', () => {
    const next = subtractWithCascade(
      { docIds: ['d1', 'd2'], refIds: ['r1'] },
      'doc',
      'd1',
      empty,
    );
    expect(next.docIds.sort()).toEqual(['d2']);
    expect(next.refIds.sort()).toEqual(['r1']);
  });

  it('first-circle ids not in selection are silently ignored', () => {
    const next = subtractWithCascade(
      { docIds: ['d1'], refIds: [] },
      'doc',
      'd1',
      { document_ids: ['d99'], reference_ids: ['r99'] },
    );
    expect(next).toEqual({ docIds: [], refIds: [] });
  });

  it('removes ref parent and its first-circle docs/refs', () => {
    const next = subtractWithCascade(
      { docIds: ['d5', 'd6'], refIds: ['r1', 'r2'] },
      'ref',
      'r1',
      { document_ids: ['d5'], reference_ids: ['r2'] },
    );
    expect(next).toEqual({ docIds: ['d6'], refIds: [] });
  });

  it('removing a child (target) does NOT remove its own deeper children when they are not in selection', () => {
    // Child d2 has its own links to d99; d99 is not in selection — no-op for it.
    const next = subtractWithCascade(
      { docIds: ['d1', 'd2'], refIds: [] },
      'doc',
      'd2',
      { document_ids: ['d99'], reference_ids: [] },
    );
    expect(next.docIds).toEqual(['d1']);
  });

  it('shared child between two parents — removing one parent removes the shared child (no claim memory)', () => {
    // A's first-circle: [X]. B is also selected and would also claim X, but we
    // intentionally do NOT consult B's links — explicit user rule.
    const next = subtractWithCascade(
      { docIds: ['A', 'B', 'X'], refIds: [] },
      'doc',
      'A',
      { document_ids: ['X'], reference_ids: [] },
    );
    expect(next.docIds.sort()).toEqual(['B']);
  });
});
