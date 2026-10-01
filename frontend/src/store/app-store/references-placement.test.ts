/** placeInGroup / addReference / placeReference — manual-order placement in the
 * references LIST.
 *
 * Pins the store half of the panel contract: the list keeps the backend's
 * depth-tier order between fetches; a live ref is (re)placed inside ITS group's
 * contiguous run by (sort_key, reference_id); a new ancestor-host ref lands at
 * the head of that host's run, never above the current doc's own group; a
 * re-hosted ref leaves the old run intact and joins the new one; archived refs
 * keep their slot (not hand-sortable).
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from 'vitest';
import { useAppStore } from '../app-store';
import { placeInGroup } from './references-slice';
import type { Reference } from '../../types';

let seq = 0;
function makeRef(overrides: Partial<Reference> = {}): Reference {
  return {
    reference_id: `ref-${++seq}`,
    project_id: 'proj-1',
    document_id: 'child',
    title: 'Ref',
    media_type: 'markdown',
    source_url: null,
    processing_status: null,
    file_path: null,
    file_meta: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

// Tier list as the backend LIST emits it: own refs (host 'child'), then the
// parent's ('parent'), then the project index's ('index') — key ASC in a tier.
function tierList(): Reference[] {
  return [
    makeRef({ reference_id: 'own-a', document_id: 'child', sort_key: 'a0' }),
    makeRef({ reference_id: 'own-b', document_id: 'child', sort_key: 'a2' }),
    makeRef({ reference_id: 'par-a', document_id: 'parent', sort_key: 'b2' }),
    makeRef({ reference_id: 'par-b', document_id: 'parent', sort_key: 'b4' }),
    makeRef({ reference_id: 'idx-a', document_id: 'index', sort_key: 'c0' }),
  ];
}

beforeEach(() => {
  seq = 0;
  useAppStore.getState().setReferences([]);
});

describe('placeInGroup (pure)', () => {
  it('re-sorts only the run and keeps tier order', () => {
    const list = tierList();
    // own-b dragged between own-a and the parent tier
    const out = placeInGroup(list, { ...list[1], sort_key: 'a1' });
    expect(out.map(r => r.reference_id)).toEqual(['own-a', 'own-b', 'par-a', 'par-b', 'idx-a']);
    // own-a dragged below own-b: only the own run re-sorts
    const out2 = placeInGroup(list, { ...list[0], sort_key: 'a5' });
    expect(out2.map(r => r.reference_id)).toEqual(['own-b', 'own-a', 'par-a', 'par-b', 'idx-a']);
  });

  it('tie-breaks equal keys by reference_id', () => {
    const list = tierList();
    const out = placeInGroup(list, { ...list[1], sort_key: 'a0' });
    expect(out.map(r => r.reference_id)).toEqual(['own-a', 'own-b', 'par-a', 'par-b', 'idx-a']);
  });

  it('a ref with no run in the list is prepended (stale-until-reload for unrelated hosts)', () => {
    const list = tierList();
    const stranger = makeRef({ reference_id: 'strange', document_id: 'stranger', sort_key: 'z0' });
    const out = placeInGroup(list, stranger);
    expect(out.map(r => r.reference_id)).toEqual(['strange', 'own-a', 'own-b', 'par-a', 'par-b', 'idx-a']);
  });

  it('an archived ref keeps its slot (patch in place, not hand-sorted)', () => {
    const list = [...tierList(), makeRef({ reference_id: 'arch', document_id: 'child', sort_key: 'zz', archived: true })];
    const out = placeInGroup(list, { ...list[5], sort_key: 'aa', archived: true });
    expect(out.map(r => r.reference_id)).toEqual(['own-a', 'own-b', 'par-a', 'par-b', 'idx-a', 'arch']);
    expect(out[5].sort_key).toBe('aa');
  });
});

describe('addReference', () => {
  it('an ancestor-host ref lands at the head of that host’s run, not above the own tier', () => {
    useAppStore.getState().setReferences(tierList());
    useAppStore.getState().addReference(
      makeRef({ reference_id: 'par-new', document_id: 'parent', sort_key: 'b1' }),
    );
    expect(useAppStore.getState().references.map(r => r.reference_id))
      .toEqual(['own-a', 'own-b', 'par-new', 'par-a', 'par-b', 'idx-a']);
  });

  it('an own-host ref (top key) lands above the existing own refs', () => {
    useAppStore.getState().setReferences(tierList());
    useAppStore.getState().addReference(
      makeRef({ reference_id: 'own-new', document_id: 'child', sort_key: 'a0V' }),
    );
    expect(useAppStore.getState().references.map(r => r.reference_id))
      .toEqual(['own-a', 'own-new', 'own-b', 'par-a', 'par-b', 'idx-a']);
  });
});

describe('placeReference', () => {
  it('a moved ref leaves its old run intact and joins the new run at its key position', () => {
    useAppStore.getState().setReferences(tierList());
    useAppStore.getState().placeReference('own-a', { document_id: 'parent', sort_key: 'b3' });
    expect(useAppStore.getState().references.map(r => r.reference_id))
      .toEqual(['own-b', 'par-a', 'own-a', 'par-b', 'idx-a']);
  });

  it('no-ops when the id is not in the list', () => {
    useAppStore.getState().setReferences(tierList());
    useAppStore.getState().placeReference('missing', { sort_key: 'a9' });
    expect(useAppStore.getState().references.map(r => r.reference_id))
      .toEqual(['own-a', 'own-b', 'par-a', 'par-b', 'idx-a']);
  });
});
