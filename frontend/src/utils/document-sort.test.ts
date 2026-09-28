import { describe, it, expect } from 'vitest';
import { buildParentMap, sortDocuments, sortDocumentsForChat, sortReferences, sortReferencesByProximity } from './document-sort';
import type { Document, Reference } from '../types';

function makeDoc(overrides: Partial<Document> & { document_id: string; title: string }): Document {
  return {
    project_id: 'proj1',
    parent_id: null,
    content: '',
    path: '',
    is_index: false,
    updated_at: '2025-01-01T00:00:00Z',
    created_at: '2025-01-01T00:00:00Z',
    ...overrides,
  };
}

function makeRef(overrides: Partial<Reference> & { reference_id: string; title: string }): Reference {
  return {
    project_id: 'proj1',
    document_id: null,
    media_type: 'markdown',
    source_url: null,
    content: '',
    processing_status: null,
    file_path: null,
    file_meta: null,
    updated_at: '2025-01-01T00:00:00Z',
    created_at: '2025-01-01T00:00:00Z',
    ...overrides,
  };
}

describe('buildParentMap', () => {
  it('builds correct map from flat document list', () => {
    const docs = [
      makeDoc({ document_id: 'a', title: 'A', parent_id: 'b' }),
      makeDoc({ document_id: 'b', title: 'B', parent_id: null }),
    ];
    const map = buildParentMap(docs);
    expect(map.get('a')).toBe('b');
    expect(map.get('b')).toBe(null);
  });

  it('handles null parent_id (root docs)', () => {
    const docs = [makeDoc({ document_id: 'root', title: 'Root', parent_id: null })];
    const map = buildParentMap(docs);
    expect(map.get('root')).toBe(null);
  });
});

describe('sortDocuments', () => {
  it('exact name match comes before starts-with', () => {
    const docs = [
      makeDoc({ document_id: '1', title: 'Alpha Beta' }),
      makeDoc({ document_id: '2', title: 'Alpha' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocuments(docs, null, 'alpha', map);
    expect(result[0].document_id).toBe('2');
    expect(result[1].document_id).toBe('1');
  });

  it('same name tier: closer tree distance wins', () => {
    const docs = [
      makeDoc({ document_id: 'root', title: 'Root', parent_id: null }),
      makeDoc({ document_id: 'sibling', title: 'Alpha A', parent_id: 'root' }),
      makeDoc({ document_id: 'current', title: 'Current', parent_id: 'root' }),
      makeDoc({ document_id: 'distant', title: 'Alpha B', parent_id: 'sibling' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocuments(
      docs.filter(d => d.title.startsWith('Alpha')),
      'current',
      'alpha',
      map,
    );
    expect(result[0].document_id).toBe('sibling');
    expect(result[1].document_id).toBe('distant');
  });

  it('same tier + distance: more recent wins', () => {
    const docs = [
      makeDoc({ document_id: 'a', title: 'Alpha', updated_at: '2025-01-01T00:00:00Z' }),
      makeDoc({ document_id: 'b', title: 'Alpha Beta', updated_at: '2025-06-01T00:00:00Z' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocuments(docs, null, 'alpha', map);
    expect(result[0].document_id).toBe('a');
    expect(result[1].document_id).toBe('b');
  });

  it('currentDocId = null: skips proximity, falls through to recency', () => {
    const docs = [
      makeDoc({ document_id: 'a', title: 'Test A', updated_at: '2025-01-01T00:00:00Z', parent_id: 'x' }),
      makeDoc({ document_id: 'b', title: 'Test B', updated_at: '2025-06-01T00:00:00Z', parent_id: null }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocuments(docs, null, 'test', map);
    expect(result[0].document_id).toBe('b');
  });

  it('empty input array', () => {
    const result = sortDocuments([], null, 'q', new Map());
    expect(result).toEqual([]);
  });

  it('single document', () => {
    const docs = [makeDoc({ document_id: 'only', title: 'Only' })];
    const map = buildParentMap(docs);
    const result = sortDocuments(docs, null, 'only', map);
    expect(result).toHaveLength(1);
    expect(result[0].document_id).toBe('only');
  });

  it('distance 0 for same document', () => {
    const docs = [
      makeDoc({ document_id: 'a', title: 'Alpha' }),
      makeDoc({ document_id: 'b', title: 'Beta' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocuments(docs, 'a', 'a', map);
    expect(result[0].document_id).toBe('a');
  });

  it('distance 2 for parent-child', () => {
    const docs = [
      makeDoc({ document_id: 'parent', title: 'Match Parent', parent_id: null }),
      makeDoc({ document_id: 'child', title: 'Match Child', parent_id: 'parent' }),
      makeDoc({ document_id: 'current', title: 'Current', parent_id: 'child' }),
    ];
    const map = buildParentMap(docs);
    const filtered = docs.filter(d => d.title.startsWith('Match'));
    const result = sortDocuments(filtered, 'current', 'Match', map);
    expect(result[0].document_id).toBe('child');
    expect(result[1].document_id).toBe('parent');
  });

  it('connects disconnected subtrees through virtual project root', () => {
    const docs = [
      makeDoc({ document_id: 'a', title: 'Alpha', parent_id: 'tree1' }),
      makeDoc({ document_id: 'b', title: 'Beta', parent_id: 'tree2' }),
      makeDoc({ document_id: 'current', title: 'Current', parent_id: 'tree3' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocuments(docs, 'current', 'a', map);
    expect(result).toHaveLength(3);
  });

  it('handles deeply nested chain', () => {
    const docs = [
      makeDoc({ document_id: 'l0', title: 'Level 0', parent_id: null }),
      makeDoc({ document_id: 'l1', title: 'Level 1', parent_id: 'l0' }),
      makeDoc({ document_id: 'l2', title: 'Level 2', parent_id: 'l1' }),
      makeDoc({ document_id: 'l3', title: 'Target', parent_id: 'l2' }),
    ];
    const map = buildParentMap(docs);
    const filtered = docs.filter(d => d.title.startsWith('Level') || d.title === 'Target');
    const result = sortDocuments(filtered, 'l2', '', map);
    expect(result[0].document_id).toBe('l2');
  });

  it('empty query gives tier 1 (starts-with) for all', () => {
    const docs = [
      makeDoc({ document_id: 'a', title: 'Apple', updated_at: '2025-01-01T00:00:00Z' }),
      makeDoc({ document_id: 'b', title: 'Banana', updated_at: '2025-06-01T00:00:00Z' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocuments(docs, null, '', map);
    expect(result[0].document_id).toBe('b');
  });

  it('handles currentDocId not in parentMap', () => {
    const docs = [
      makeDoc({ document_id: 'a', title: 'Alpha' }),
      makeDoc({ document_id: 'b', title: 'Beta' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocuments(docs, 'orphan', 'a', map);
    expect(result).toHaveLength(2);
    expect(result[0].document_id).toBe('a');
  });
});

describe('sortReferences', () => {
  it('name match tier ordering', () => {
    const refs = [
      makeRef({ reference_id: '1', title: 'Alpha Beta' }),
      makeRef({ reference_id: '2', title: 'Alpha' }),
    ];
    const result = sortReferences(refs, 'alpha');
    expect(result[0].reference_id).toBe('2');
    expect(result[1].reference_id).toBe('1');
  });

  it('recency fallback when same tier', () => {
    const refs = [
      makeRef({ reference_id: 'a', title: 'Alpha', updated_at: '2025-01-01T00:00:00Z' }),
      makeRef({ reference_id: 'b', title: 'Alpha Beta', updated_at: '2025-06-01T00:00:00Z' }),
    ];
    const result = sortReferences(refs, 'alpha');
    expect(result[0].reference_id).toBe('a');
    expect(result[1].reference_id).toBe('b');
  });

  it('empty input array', () => {
    const result = sortReferences([], 'query');
    expect(result).toEqual([]);
  });
});

describe('sortDocumentsForChat', () => {
  it('anchor document comes first', () => {
    const docs = [
      makeDoc({ document_id: 'other', title: 'Other' }),
      makeDoc({ document_id: 'anchor', title: 'Anchor' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocumentsForChat(docs, 'anchor', map);
    expect(result[0].document_id).toBe('anchor');
  });

  it('orders by tree distance ascending from anchor', () => {
    const docs = [
      makeDoc({ document_id: 'root', title: 'Root', parent_id: null }),
      makeDoc({ document_id: 'anchor', title: 'Anchor', parent_id: 'root' }),
      makeDoc({ document_id: 'sibling', title: 'Sibling', parent_id: 'root' }),
      makeDoc({ document_id: 'cousin', title: 'Cousin', parent_id: 'sibling' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocumentsForChat(docs, 'anchor', map);
    expect(result.map(d => d.document_id)).toEqual(['anchor', 'root', 'sibling', 'cousin']);
  });

  it('descendants beat non-descendants at same tree distance', () => {
    // anchor's child (distance 1) vs anchor's parent (distance 1) — child wins
    const docs = [
      makeDoc({ document_id: 'parent', title: 'Parent', parent_id: null }),
      makeDoc({ document_id: 'anchor', title: 'Anchor', parent_id: 'parent' }),
      makeDoc({ document_id: 'child', title: 'Child', parent_id: 'anchor' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocumentsForChat(docs, 'anchor', map);
    expect(result.map(d => d.document_id)).toEqual(['anchor', 'child', 'parent']);
  });

  it('falls back to sort_key ASC at same distance and kinship (tree order)', () => {
    // updated_at is intentionally inverse to sort_key to prove sort_key wins.
    const docs = [
      makeDoc({ document_id: 'root', title: 'Root', parent_id: null, sort_key: 'a0' }),
      makeDoc({ document_id: 'anchor', title: 'Anchor', parent_id: 'root', sort_key: 'a1' }),
      makeDoc({ document_id: 'first', title: 'B First', parent_id: 'root', sort_key: 'a2', updated_at: '2025-01-01T00:00:00Z' }),
      makeDoc({ document_id: 'second', title: 'A Second', parent_id: 'root', sort_key: 'a5', updated_at: '2025-06-01T00:00:00Z' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocumentsForChat(docs, 'anchor', map);
    expect(result[0].document_id).toBe('anchor');
    expect(result[1].document_id).toBe('root');
    expect(result[2].document_id).toBe('first');
    expect(result[3].document_id).toBe('second');
  });

  it('falls back to document_id at same sort_key', () => {
    const docs = [
      makeDoc({ document_id: 'root', title: 'Root', parent_id: null, sort_key: 'a0' }),
      makeDoc({ document_id: 'anchor', title: 'Anchor', parent_id: 'root', sort_key: 'a1' }),
      makeDoc({ document_id: 'b', title: 'Beta', parent_id: 'root', sort_key: 'a5' }),
      makeDoc({ document_id: 'a', title: 'alpha', parent_id: 'root', sort_key: 'a5' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocumentsForChat(docs, 'anchor', map);
    expect(result.map(d => d.document_id)).toEqual(['anchor', 'root', 'a', 'b']);
  });

  it('null anchor: sort by sort_key ASC', () => {
    const docs = [
      makeDoc({ document_id: 'a', title: 'Alpha', sort_key: 'a5', updated_at: '2025-01-01T00:00:00Z' }),
      makeDoc({ document_id: 'b', title: 'Beta', sort_key: 'a1', updated_at: '2025-06-01T00:00:00Z' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocumentsForChat(docs, null, map);
    expect(result.map(d => d.document_id)).toEqual(['b', 'a']);
  });

  it('disconnected subtree: still sorts the docs, anchor first', () => {
    const docs = [
      makeDoc({ document_id: 'anchor', title: 'Anchor', parent_id: null }),
      makeDoc({ document_id: 'foreign', title: 'Foreign', parent_id: 'unknown' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocumentsForChat(docs, 'anchor', map);
    expect(result[0].document_id).toBe('anchor');
    expect(result[1].document_id).toBe('foreign');
  });

  it('foreign-tree depth ordering via virtual project root', () => {
    // Tree:
    // A
    //   B (anchor)
    //   C
    // D
    //   E
    //     F
    const docs = [
      makeDoc({ document_id: 'A', title: 'A', parent_id: null }),
      makeDoc({ document_id: 'B', title: 'B', parent_id: 'A' }),
      makeDoc({ document_id: 'C', title: 'C', parent_id: 'A' }),
      makeDoc({ document_id: 'D', title: 'D', parent_id: null }),
      makeDoc({ document_id: 'E', title: 'E', parent_id: 'D' }),
      makeDoc({ document_id: 'F', title: 'F', parent_id: 'E' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocumentsForChat(docs, 'B', map);
    expect(result.map(d => d.document_id)).toEqual(['B', 'A', 'C', 'D', 'E', 'F']);
  });

  it('name-match tiebreaker at same tree distance and kinship', () => {
    const docs = [
      makeDoc({ document_id: 'root', title: 'Root', parent_id: null }),
      makeDoc({ document_id: 'anchor', title: 'Anchor', parent_id: 'root' }),
      makeDoc({ document_id: 'exact', title: 'Alpha', parent_id: 'root', updated_at: '2025-01-01T00:00:00Z' }),
      makeDoc({ document_id: 'prefix', title: 'Alpha Beta', parent_id: 'root', updated_at: '2025-06-01T00:00:00Z' }),
      makeDoc({ document_id: 'contains', title: 'X-Alpha-Y', parent_id: 'root', updated_at: '2025-09-01T00:00:00Z' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocumentsForChat(docs, 'anchor', map, 'alpha');
    expect(result[0].document_id).toBe('anchor');
    expect(result[1].document_id).toBe('root');
    expect(result[2].document_id).toBe('exact');
    expect(result[3].document_id).toBe('prefix');
    expect(result[4].document_id).toBe('contains');
  });

  it('null anchor with query: name-match ordering', () => {
    const docs = [
      makeDoc({ document_id: 'exact', title: 'Alpha', updated_at: '2025-01-01T00:00:00Z' }),
      makeDoc({ document_id: 'prefix', title: 'Alpha Beta', updated_at: '2025-06-01T00:00:00Z' }),
      makeDoc({ document_id: 'contains', title: 'X-Alpha-Y', updated_at: '2025-09-01T00:00:00Z' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocumentsForChat(docs, null, map, 'alpha');
    expect(result[0].document_id).toBe('exact');
    expect(result[1].document_id).toBe('prefix');
    expect(result[2].document_id).toBe('contains');
  });

  it('null anchor without query: sort_key ASC (tree order)', () => {
    const docs = [
      makeDoc({ document_id: 'a', title: 'Alpha', sort_key: 'a5', updated_at: '2025-06-01T00:00:00Z' }),
      makeDoc({ document_id: 'b', title: 'Beta', sort_key: 'a1', updated_at: '2025-01-01T00:00:00Z' }),
    ];
    const map = buildParentMap(docs);
    const result = sortDocumentsForChat(docs, null, map, '');
    expect(result.map(d => d.document_id)).toEqual(['b', 'a']);
  });
});

describe('sortReferencesByProximity', () => {
  // Tree used across cases:
  //   root
  //   ├─ anchor  (anchorDocId)
  //   │   └─ child
  //   └─ sibling
  function tree() {
    const docs = [
      makeDoc({ document_id: 'root', title: 'Root', parent_id: null }),
      makeDoc({ document_id: 'anchor', title: 'Anchor', parent_id: 'root' }),
      makeDoc({ document_id: 'child', title: 'Child', parent_id: 'anchor' }),
      makeDoc({ document_id: 'sibling', title: 'Sibling', parent_id: 'root' }),
    ];
    return buildParentMap(docs);
  }

  it('selected references sort FIRST even when an unselected ref is nearer and an exact name match', () => {
    const map = tree();
    const refs = [
      // unselected, owned by the anchor itself (distance 0), exact name match
      makeRef({ reference_id: 'near', title: 'Alpha', document_id: 'anchor', updated_at: '2025-06-01T00:00:00Z' }),
      // selected, owned by sibling (farther), non-matching title
      makeRef({ reference_id: 'sel', title: 'Selected', document_id: 'sibling', updated_at: '2025-01-01T00:00:00Z' }),
    ];
    const result = sortReferencesByProximity(refs, 'anchor', null, new Set(['sel']), map, 'alpha');
    expect(result.map(r => r.reference_id)).toEqual(['sel', 'near']);
  });

  it('the anchor reference sorts next (after selected, before proximity) when not selected', () => {
    const map = tree();
    const refs = [
      makeRef({ reference_id: 'anchorRef', title: 'ARef', document_id: 'sibling' }),
      makeRef({ reference_id: 'near', title: 'Near', document_id: 'anchor' }),
      makeRef({ reference_id: 'far', title: 'Far', document_id: 'sibling' }),
    ];
    // None selected. anchorRef is the anchor ref but owned by sibling (distance 2);
    // near is owned by anchor (distance 0). Anchor-ref tier beats proximity.
    const result = sortReferencesByProximity(refs, 'anchor', 'anchorRef', new Set(), map, '');
    expect(result[0].reference_id).toBe('anchorRef');
  });

  it('nearer parent docs come before farther ones (no query, none selected)', () => {
    const map = tree();
    const refs = [
      makeRef({ reference_id: 'far', title: 'Far', document_id: 'sibling', updated_at: '2025-09-01T00:00:00Z' }),
      makeRef({ reference_id: 'self', title: 'Self', document_id: 'anchor', updated_at: '2025-01-01T00:00:00Z' }),
      makeRef({ reference_id: 'child', title: 'Child', document_id: 'child', updated_at: '2025-01-01T00:00:00Z' }),
    ];
    const result = sortReferencesByProximity(refs, 'anchor', null, new Set(), map, '');
    // distance: self=0, child=2 (anchor→child... wait child is anchor's child)
    // anchor(0) → child is a descendant: treeDistance(anchor, child) walks child→anchor=0? trace below
    expect(result.map(r => r.reference_id)).toEqual(['self', 'child', 'far']);
  });

  it('newest first within the same owning document (same distance)', () => {
    const map = tree();
    const refs = [
      makeRef({ reference_id: 'old', title: 'R', document_id: 'anchor', updated_at: '2025-01-01T00:00:00Z' }),
      makeRef({ reference_id: 'new', title: 'R', document_id: 'anchor', updated_at: '2025-06-01T00:00:00Z' }),
    ];
    const result = sortReferencesByProximity(refs, 'anchor', null, new Set(), map, '');
    expect(result.map(r => r.reference_id)).toEqual(['new', 'old']);
  });

  it('query promotes name-match within a proximity tier', () => {
    const map = tree();
    const refs = [
      makeRef({ reference_id: 'contains', title: 'X Alpha Y', document_id: 'anchor', updated_at: '2025-09-01T00:00:00Z' }),
      makeRef({ reference_id: 'exact', title: 'Alpha', document_id: 'anchor', updated_at: '2025-01-01T00:00:00Z' }),
    ];
    // Same distance (both owned by anchor); with query, exact match (tier 0) beats contains (tier 2)
    // even though contains is newer.
    const result = sortReferencesByProximity(refs, 'anchor', null, new Set(), map, 'alpha');
    expect(result.map(r => r.reference_id)).toEqual(['exact', 'contains']);
  });

  it('document_id === null (project-level ref): treated as project-root distance, never hidden', () => {
    const map = tree();
    const refs = [
      makeRef({ reference_id: 'projectRef', title: 'Project', document_id: null }),
      makeRef({ reference_id: 'self', title: 'Self', document_id: 'anchor' }),
    ];
    const result = sortReferencesByProximity(refs, 'anchor', null, new Set(), map, '');
    // project-level ref is farther than the anchor's own ref → sorts after, but present.
    expect(result).toHaveLength(2);
    expect(result[0].reference_id).toBe('self');
    expect(result[1].reference_id).toBe('projectRef');
  });

  it('anchorDocId === null: distance tier skipped (selected → anchor → name → date)', () => {
    const map = tree();
    const refs = [
      makeRef({ reference_id: 'r1', title: 'R1', document_id: 'anchor', updated_at: '2025-01-01T00:00:00Z' }),
      makeRef({ reference_id: 'r2', title: 'R2', document_id: 'sibling', updated_at: '2025-06-01T00:00:00Z' }),
      makeRef({ reference_id: 'sel', title: 'Selected', document_id: 'child', updated_at: '2025-01-01T00:00:00Z' }),
    ];
    // No anchor doc → no distance. Selected first, then anchor ref, then by date.
    const result = sortReferencesByProximity(refs, null, 'r2', new Set(['sel']), map, '');
    expect(result[0].reference_id).toBe('sel');   // selected tier
    expect(result[1].reference_id).toBe('r2');    // anchor ref tier
    // r1 vs r3 by date — neither is r2; r1 updated earlier, but no query so date DESC.
  });

  it('empty input', () => {
    expect(sortReferencesByProximity([], 'anchor', null, new Set(), new Map(), '')).toEqual([]);
  });
});
