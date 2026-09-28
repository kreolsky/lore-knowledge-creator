import { describe, it, expect } from 'vitest';
import { ancestorIds } from './document-ancestors';
import type { Document } from '../types';

function makeDoc(overrides: Partial<Document> & { document_id: string }): Document {
  return {
    project_id: 'p1',
    parent_id: null,
    title: 'T',
    content: '',
    path: '',
    is_index: false,
    created_at: '',
    updated_at: '',
    ...overrides,
  };
}

describe('ancestorIds', () => {
  it('returns [] for a root document (parent_id null)', () => {
    const docs = [makeDoc({ document_id: 'root' })];
    expect(ancestorIds(docs, 'root')).toEqual([]);
  });

  it('returns [] for an unknown id (not in the list)', () => {
    const docs = [makeDoc({ document_id: 'a' })];
    expect(ancestorIds(docs, 'missing')).toEqual([]);
  });

  it('returns the chain root-first for a nested document', () => {
    const docs = [
      makeDoc({ document_id: 'root' }),
      makeDoc({ document_id: 'mid', parent_id: 'root' }),
      makeDoc({ document_id: 'leaf', parent_id: 'mid' }),
    ];
    expect(ancestorIds(docs, 'leaf')).toEqual(['root', 'mid']);
  });

  it('stops the walk at a missing parent row (no throw, empty chain)', () => {
    // leaf → mid (not loaded). A missing ancestor can't be expanded, so the walk
    // stops before adding it and yields [] (leaf is treated as effectively root-level
    // for reveal — its loaded ancestors are none).
    const docs = [
      makeDoc({ document_id: 'leaf', parent_id: 'mid' }),
    ];
    expect(ancestorIds(docs, 'leaf')).toEqual([]);
  });

  it('is cycle-guarded (parent chain loop terminates)', () => {
    // a ↔ b cycle (impossible in valid data, but must not hang)
    const docs = [
      makeDoc({ document_id: 'a', parent_id: 'b' }),
      makeDoc({ document_id: 'b', parent_id: 'a' }),
      makeDoc({ document_id: 'leaf', parent_id: 'a' }),
    ];
    const chain = ancestorIds(docs, 'leaf');
    // No infinite loop; each ancestor appears at most once.
    expect(new Set(chain).size).toBe(chain.length);
    expect(chain).toContain('a');
    expect(chain).toContain('b');
  });
});
