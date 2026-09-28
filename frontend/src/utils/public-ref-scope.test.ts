/**
 * publicPanelReferences — pure scope projection for the public-share panel.
 *
 * Locks the display-time contract (plan public-refs-panel-scope-parity):
 * on /docs/<id> the panel renders ONLY refs owned by {currentDoc} ∪ its
 * in-subtree ancestor chain, PRESERVING the backend depth-tier order exactly
 * (own batch newest-first → parent batch → … → share root). The store list
 * stays whole-subtree (public transclusion seeds from it) — the filter is a
 * pure projection for rendering only: no mutation, no re-sort.
 */
import { describe, it, expect } from 'vitest';
import type { Document, Reference } from '../types';
import { publicPanelReferences } from './public-ref-scope';

const NOW = '2026-01-01T00:00:00Z';

function mkDoc(id: string, parent_id: string | null): Document {
  return {
    document_id: id, project_id: '', parent_id, title: id, content: '', path: '',
    is_index: false, created_at: NOW, updated_at: NOW,
  };
}

function mkRef(id: string, document_id: string | null): Reference {
  return {
    reference_id: id, project_id: '', document_id, title: id, media_type: 'markdown',
    source_url: null, processing_status: null, file_path: null, file_meta: null,
    created_at: NOW, updated_at: NOW,
  };
}

// Tree: root → A → A1 → A1child, root → B (sibling branch).
const documents: Document[] = [
  mkDoc('root', null),
  mkDoc('A', 'root'),
  mkDoc('A1', 'A'),
  mkDoc('A1child', 'A1'),
  mkDoc('B', 'root'),
];

// Payload exactly as the backend ships it for doc A1: depth-tier order for the
// own+ancestor subset, then every other subtree ref in the flat time tier.
const payload: Reference[] = [
  mkRef('a1-new', 'A1'),
  mkRef('a1-old', 'A1'),
  mkRef('a-ref', 'A'),
  mkRef('root-ref', 'root'),
  mkRef('b-ref', 'B'),
  mkRef('a1child-ref', 'A1child'),
];

describe('publicPanelReferences (pure projection)', () => {
  it('keeps own + ancestor refs, drops sibling/descendant/foreign refs', () => {
    const out = publicPanelReferences(payload, documents, 'A1');
    expect(out.map(r => r.reference_id)).toEqual(['a1-new', 'a1-old', 'a-ref', 'root-ref']);
  });

  it('preserves the incoming (backend-authoritative) order exactly — filter only, never a re-sort', () => {
    // Reversed payload: whatever order arrives must survive verbatim.
    const reversed = [...payload].reverse();
    const out = publicPanelReferences(reversed, documents, 'A1');
    expect(out.map(r => r.reference_id)).toEqual(['root-ref', 'a-ref', 'a1-old', 'a1-new']);
  });

  it('keeps the SAME object identities (pure projection — no cloning, no mutation)', () => {
    const input = [...payload];
    const out = publicPanelReferences(input, documents, 'A1');
    out.forEach((r, i) => expect(r).toBe(input.find(x => x.reference_id === out[i].reference_id)));
    expect(input).toEqual(payload); // input untouched
    expect(input).toHaveLength(6);  // store list shape unchanged
  });

  it('share-root doc sees only its own refs (its ancestor chain is empty)', () => {
    const out = publicPanelReferences(payload, documents, 'root');
    expect(out.map(r => r.reference_id)).toEqual(['root-ref']);
  });

  it('mid-tree doc sees own + parent + share-root batches', () => {
    const out = publicPanelReferences(payload, documents, 'A');
    expect(out.map(r => r.reference_id)).toEqual(['a-ref', 'root-ref']);
  });

  it('returns [] for a null or unknown docId (no scope to project)', () => {
    expect(publicPanelReferences(payload, documents, null)).toEqual([]);
    expect(publicPanelReferences(payload, documents, 'nope')).toEqual([]);
  });

  it('drops refs with a null document_id (project-level refs never exist on the public payload)', () => {
    const withNull = [...payload, mkRef('null-ref', null)];
    const out = publicPanelReferences(withNull, documents, 'A1');
    expect(out.map(r => r.reference_id)).not.toContain('null-ref');
  });

  it('stops the ancestor walk at the share root (root parent_id null — no out-of-scope leak)', () => {
    // The walk must terminate via the null parent, not by needing an out-of-payload parent.
    const out = publicPanelReferences(payload, documents, 'A1');
    expect(out.map(r => r.reference_id)).toContain('root-ref');
  });

  it('is cycle-safe (a malformed tree terminates instead of hanging)', () => {
    const cyclic: Document[] = [
      { ...mkDoc('x', 'y') },
      { ...mkDoc('y', 'x') },
    ];
    const refs = [mkRef('r-x', 'x'), mkRef('r-y', 'y')];
    // Termination + own-ref retention is the contract; membership beyond the
    // starting doc under a malformed cycle is unspecified (ancestorIds domain).
    const out = publicPanelReferences(refs, cyclic, 'x');
    expect(out.map(r => r.reference_id)).toContain('r-x');
  });
});
