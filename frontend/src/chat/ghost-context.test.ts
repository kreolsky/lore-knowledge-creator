/**
 * Spec for the DERIVED ghost context.
 *
 * Ghost (zero-chat) auto-context is a pure function of (open entity, split,
 * linkCache, manual deltas). These tests pin the selector + the
 * ephemeral delta store. The selector is order-independent and race-free by
 * construction — there is no stored bucket, no merge, no epoch.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('../api/client', () => ({ apiClient: { patch: vi.fn(() => Promise.resolve({})) } }));
vi.mock('../api/links', () => ({
  fetchDocumentLinksFresh: vi.fn(),
  fetchReferenceLinksFresh: vi.fn(),
  // Real Map so getDerivedGhostContext (not exercised here) would still compile.
  linkCache: new Map(),
}));

import {
  deriveGhostContext,
  computeGhostBaseKey,
  getGhostDeltas,
  syncGhostBaseKey,
  addGhostDelta,
  removeGhostDelta,
  resetGhostDeltas,
  __resetGhostDeltaStore,
  type GhostDeriveInput,
} from './context';
import { refIsScope } from '../store/ui-store/documents-slice';

const EMPTY_DELTAS = { addedDocIds: [], addedRefIds: [], removedDocIds: [], removedRefIds: [] };

function input(over: Partial<GhostDeriveInput> = {}): GhostDeriveInput {
  return {
    docId: null,
    refId: null,
    splitActive: false,
    linkCache: new Map(),
    deltas: { ...EMPTY_DELTAS },
    ...over,
  };
}

describe('deriveGhostContext — base (no deltas, cold cache)', () => {
  it('non-split: open doc only → bare doc id (first-circle absent while cold)', () => {
    expect(deriveGhostContext(input({ docId: 'doc-1' }))).toEqual({ docIds: ['doc-1'], refIds: [] });
  });

  it('non-split: ref preferred over doc when both open', () => {
    expect(deriveGhostContext(input({ docId: 'doc-1', refId: 'ref-1' }))).toEqual({
      docIds: [], refIds: ['ref-1'],
    });
  });

  it('nothing open → empty (fresh empty ghost)', () => {
    expect(deriveGhostContext(input())).toEqual({ docIds: [], refIds: [] });
  });

  it('cold cache: base entity present, first-circle absent → only the bare id', () => {
    const r = deriveGhostContext(input({ docId: 'doc-1' }));
    expect(r).toEqual({ docIds: ['doc-1'], refIds: [] });
  });

  it('warm cache: first-circle unioned onto the base doc', () => {
    const cache = new Map([
      ['doc:doc-1', { document_ids: ['linked-doc'], reference_ids: ['linked-ref'] }],
    ]);
    expect(deriveGhostContext(input({ docId: 'doc-1', linkCache: cache }))).toEqual({
      docIds: ['doc-1', 'linked-doc'], refIds: ['linked-ref'],
    });
  });

  it('warm cache: recompute after warming includes first-circle (recompute, not merge)', () => {
    const cache = new Map();
    const i = input({ docId: 'doc-1', linkCache: cache });
    expect(deriveGhostContext(i)).toEqual({ docIds: ['doc-1'], refIds: [] });
    cache.set('doc:doc-1', { document_ids: ['linked-doc'], reference_ids: ['linked-ref'] });
    expect(deriveGhostContext(i)).toEqual({
      docIds: ['doc-1', 'linked-doc'], refIds: ['linked-ref'],
    });
  });
});

describe('deriveGhostContext — split view', () => {
  it('split ON: both doc + ref + both first-circles', () => {
    const cache = new Map([
      ['doc:d', { document_ids: ['d-l'], reference_ids: [] }],
      ['ref:r', { document_ids: [], reference_ids: ['r-l'] }],
    ]);
    expect(deriveGhostContext(input({ docId: 'd', refId: 'r', splitActive: true, linkCache: cache })))
      .toEqual({ docIds: ['d', 'd-l'], refIds: ['r', 'r-l'] });
  });

  it('split ON: only doc pane present → doc only', () => {
    expect(deriveGhostContext(input({ docId: 'd', refId: null, splitActive: true })))
      .toEqual({ docIds: ['d'], refIds: [] });
  });

  it('split ON: only ref pane present → ref only', () => {
    expect(deriveGhostContext(input({ docId: null, refId: 'r', splitActive: true })))
      .toEqual({ docIds: [], refIds: ['r'] });
  });
});

describe('deriveGhostContext — base always included (unconditional)', () => {
  // WHY (ghost-context.ts): the open entity is in context UNCONDITIONALLY.
  // The old attachOpenEntityToGhost attached it regardless of any flag; a flag
  // never existed for a plain open doc — gating would silently drop it.
  it('open doc is included with no extra flag (no silent drop)', () => {
    expect(deriveGhostContext(input({ docId: 'doc-1' }))).toEqual({ docIds: ['doc-1'], refIds: [] });
  });

  it('split: both panes included (each pane always counts as context)', () => {
    expect(deriveGhostContext(input({ docId: 'doc-1', refId: 'ref-1', splitActive: true })))
      .toEqual({ docIds: ['doc-1'], refIds: ['ref-1'] });
  });
});

describe('deriveGhostContext — manual deltas', () => {
  it('add an extra doc → present (+ its cached first-circle)', () => {
    const cache = new Map([['doc:extra', { document_ids: ['x-l'], reference_ids: [] }]]);
    expect(deriveGhostContext(input({
      docId: 'doc-1', linkCache: cache,
      deltas: { ...EMPTY_DELTAS, addedDocIds: ['extra'] },
    }))).toEqual({ docIds: ['doc-1', 'extra', 'x-l'], refIds: [] });
  });

  it('remove a base doc → absent (and its first-circle subtracted)', () => {
    const cache = new Map([['doc:doc-1', { document_ids: ['d-l'], reference_ids: [] }]]);
    expect(deriveGhostContext(input({
      docId: 'doc-1', linkCache: cache,
      deltas: { ...EMPTY_DELTAS, removedDocIds: ['doc-1'] },
    }))).toEqual({ docIds: [], refIds: [] });
  });

  it('add then remove the same delta id → net absent', () => {
    expect(deriveGhostContext(input({
      docId: 'doc-1',
      deltas: { addedDocIds: ['extra'], removedDocIds: ['extra'], addedRefIds: [], removedRefIds: [] },
    }))).toEqual({ docIds: ['doc-1'], refIds: [] });
  });
});

describe('deriveGhostContext — order-independence (over-inclusion cannot recur)', () => {
  it('calling twice yields identical results (no stored/merge state)', () => {
    const cache = new Map([['doc:d', { document_ids: ['d-l'], reference_ids: ['r-l'] }]]);
    const i = input({ docId: 'd', linkCache: cache, deltas: { ...EMPTY_DELTAS, addedDocIds: ['x'] } });
    const a = deriveGhostContext(i);
    const b = deriveGhostContext(i);
    expect(a).toEqual(b);
    expect(a).toEqual({ docIds: ['d', 'd-l', 'x'], refIds: ['r-l'] });
  });

  // The original report: a lone reference open (no links) → ghost == [that ref]
  // only — no parent doc, no parent first-circle leaks in.
  it('lone reference (no links) → only that reference, no parent doc leak', () => {
    expect(deriveGhostContext(input({ docId: 'parent-doc', refId: 'the-ref' })))
      .toEqual({ docIds: [], refIds: ['the-ref'] });
  });
});

// Panel quick preview: the ghost input layer (use-ghost-context + the chat-store
// bridge) reads the open reference through refIsScope BEFORE it reaches these
// pure functions — a previewed ref arrives as refId=null, so the ghost base is
// the document's. Pins the projection + its composition with the selector.
describe('panel quick preview — projected ref input', () => {
  it('refIsScope: panel is NOT a scope mode; center/split are', () => {
    expect(refIsScope('panel')).toBe(false);
    expect(refIsScope('center')).toBe(true);
    expect(refIsScope('split')).toBe(true);
  });

  it('panel mode with an open ref → refIds empty, docIds = [doc] (splitActive true, projected refId null)', () => {
    const refId = refIsScope('panel') ? 'ref-1' : null;
    expect(refId).toBeNull();
    expect(deriveGhostContext(input({ docId: 'doc-1', refId, splitActive: true })))
      .toEqual({ docIds: ['doc-1'], refIds: [] });
  });

  it('panel: baseKey of the projected input is the doc only (mode flip resets deltas via refId)', () => {
    expect(computeGhostBaseKey('doc-1', refIsScope('panel') ? 'ref-1' : null, true)).toBe('doc-1|');
  });

  it('split: both doc and ref stay in the base (scope mode, unchanged)', () => {
    const refId = refIsScope('split') ? 'ref-1' : null;
    expect(deriveGhostContext(input({ docId: 'doc-1', refId, splitActive: true })))
      .toEqual({ docIds: ['doc-1'], refIds: ['ref-1'] });
  });

  it('center: lone ref still prefers the ref (unchanged)', () => {
    const refId = refIsScope('center') ? 'ref-1' : null;
    expect(deriveGhostContext(input({ docId: 'doc-1', refId, splitActive: false })))
      .toEqual({ docIds: [], refIds: ['ref-1'] });
  });
});

describe('computeGhostBaseKey', () => {
  it('encodes the ghost BASE identity (docs|refs), not the raw doc|ref|split tuple', () => {
    // split: both doc + ref in base.
    expect(computeGhostBaseKey('d', 'r', true)).toBe('d|r');
    // nothing open → empty base.
    expect(computeGhostBaseKey(null, null, false)).toBe('|');
    // doc only → doc in base.
    expect(computeGhostBaseKey('d', null, false)).toBe('d|');
  });

  // Regression (review finding: baseKey must match derive's base, not the raw tuple).
  // Non-split with a reference: base is the ref only. Switching the BACKGROUND doc
  // (ref unchanged) must NOT change baseKey → must NOT reset manual deltas.
  it('non-split with ref: switching the background doc does not change baseKey', () => {
    expect(computeGhostBaseKey('docA', 'refX', false)).toBe('|refX');
    expect(computeGhostBaseKey('docB', 'refX', false)).toBe('|refX');
  });
});

describe('ghost delta store', () => {
  beforeEach(() => __resetGhostDeltaStore());

  it('add/remove mutate the derived fold correctly', () => {
    addGhostDelta('doc', 'extra');
    expect(getGhostDeltas().addedDocIds).toEqual(['extra']);
    removeGhostDelta('doc', 'extra');
    expect(getGhostDeltas().addedDocIds).toEqual([]);
    expect(getGhostDeltas().removedDocIds).toEqual(['extra']);
  });

  it('add clears a prior remove of the same id (re-add a removed base)', () => {
    removeGhostDelta('ref', 'r1');
    addGhostDelta('ref', 'r1');
    expect(getGhostDeltas().removedRefIds).toEqual([]);
    expect(getGhostDeltas().addedRefIds).toEqual(['r1']);
  });

  it('resetGhostDeltas clears deltas but keeps baseKey', () => {
    syncGhostBaseKey('d|r|false');
    addGhostDelta('doc', 'x');
    resetGhostDeltas();
    expect(getGhostDeltas().addedDocIds).toEqual([]);
  });

  it('syncGhostBaseKey RESETS deltas when the open entity changes', () => {
    syncGhostBaseKey('docA||false');
    addGhostDelta('doc', 'pick-A');
    expect(getGhostDeltas().addedDocIds).toEqual(['pick-A']);
    // Navigate to a different open entity → deltas relative to the old base are gone.
    syncGhostBaseKey('docB||false');
    expect(getGhostDeltas().addedDocIds).toEqual([]);
  });

  it('syncGhostBaseKey is a no-op when the entity is unchanged (preserves picks)', () => {
    syncGhostBaseKey('docA||false');
    addGhostDelta('doc', 'pick-A');
    syncGhostBaseKey('docA||false');
    expect(getGhostDeltas().addedDocIds).toEqual(['pick-A']);
  });
});
