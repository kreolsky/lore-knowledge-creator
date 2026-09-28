/**
 * Unit tests for resolveReferenceNav — the pure decision function extracted from
 * useEditorEvents' navigate-to-reference handler.
 *
 * Why this exists: the cross-doc vs same-doc classification is the crux of the
 * ref-link navigation revert(root causes 1). Previously
 * it lived inline in a React hook with no unit-test harness, so the handler-level
 * routing had ZERO automated coverage — a re-introduced unguarded setCurrentReference
 * (root cause 2) or a mis-classified ancestor-owned ref (root cause 1) would not be
 * caught. Extracting the decision makes every branch directly unit-testable.
 */
import { describe, it, expect } from 'vitest';
import { resolveReferenceNav } from './resolveReferenceNav';
import type { Reference } from '../types';

function makeRef(overrides: Partial<Reference> = {}): Reference {
  return {
    reference_id: 'ref-1',
    project_id: 'proj-1',
    document_id: null,
    title: 'Test Ref',
    media_type: 'markdown',
    source_url: null,
    content: '',
    processing_status: null,
    file_path: null,
    file_meta: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

describe('resolveReferenceNav', () => {
  it('returns noop when ref is null', () => {
    const d = resolveReferenceNav({ ref: null, currentDocId: 'doc-A', stayInContext: false });
    expect(d.kind).toBe('noop');
  });

  it('returns stay-in-context when stayInContext is set, even if ref is owned by another doc', () => {
    // stayInContext must short-circuit BEFORE the cross-doc check — it populates the
    // right column in split view without ever navigating (useEditorEvents invariant).
    const ref = makeRef({ document_id: 'doc-B' });
    const d = resolveReferenceNav({ ref, currentDocId: 'doc-A', stayInContext: true });
    expect(d.kind).toBe('stay-in-context');
  });

  it('returns cross-doc + targetDocId when ref is owned by a different document', () => {
    const ref = makeRef({ document_id: 'doc-B', reference_id: 'ref-X' });
    const d = resolveReferenceNav({ ref, currentDocId: 'doc-A', stayInContext: false });
    expect(d.kind).toBe('cross-doc');
    if (d.kind === 'cross-doc') expect(d.targetDocId).toBe('doc-B');
  });

  it('returns same-doc when ref is owned by the current document', () => {
    const ref = makeRef({ document_id: 'doc-A' });
    expect(resolveReferenceNav({ ref, currentDocId: 'doc-A', stayInContext: false }).kind).toBe('same-doc');
  });

  // Regression for root cause 1: ancestor-scoped refs clicked from a child doc are
  // CROSS-DOC even though the user perceives them as "in my document". References are
  // ancestor-scoped (backend/routes/references.py get_ancestor_ids), so a ref visible
  // in D1's panel frequently has document_id === <ancestor> !== D1.
  it('classifies an ancestor-owned ref clicked from a child doc as cross-doc', () => {
    const ancestorRef = makeRef({ document_id: 'doc-ancestor', reference_id: 'ref-anc' });
    const d = resolveReferenceNav({ ref: ancestorRef, currentDocId: 'doc-child', stayInContext: false });
    expect(d.kind).toBe('cross-doc');
    if (d.kind === 'cross-doc') expect(d.targetDocId).toBe('doc-ancestor');
  });

  it('returns cross-doc when currentDocId is undefined and ref has a document_id (cold start)', () => {
    // No current doc loaded yet, but the ref belongs to some doc → navigate to it.
    const ref = makeRef({ document_id: 'doc-B' });
    expect(resolveReferenceNav({ ref, currentDocId: undefined, stayInContext: false }).kind).toBe('cross-doc');
  });

  it('returns same-doc when ref.document_id is null (no owning doc, no navigation possible)', () => {
    // Degenerate: ref has no owning doc. There is nothing to navigate to; treat as
    // same-doc (the handler's side effects are no-ops; hydrate already committed).
    const ref = makeRef({ document_id: null });
    expect(resolveReferenceNav({ ref, currentDocId: 'doc-A', stayInContext: false }).kind).toBe('same-doc');
  });

  it('returns same-doc when ref.document_id is null AND currentDocId is undefined', () => {
    const ref = makeRef({ document_id: null });
    expect(resolveReferenceNav({ ref, currentDocId: undefined, stayInContext: false }).kind).toBe('same-doc');
  });

  it('treats an empty-string document_id as same-doc (falsy guard, no navigation)', () => {
    // Mirrors the handler's `if (parentDocId && ...)` — a falsy owning doc never
    // triggers the cross-doc branch.
    const ref = makeRef({ document_id: '' });
    expect(resolveReferenceNav({ ref, currentDocId: 'doc-A', stayInContext: false }).kind).toBe('same-doc');
  });

  // Panel quick preview: the document is the scope — every ref (whatever its
  // owning doc) opens inside the Refs tab, so the resolver answers
  // stay-in-context and the cross-doc navigation branch never fires.
  describe('panel quick preview', () => {
    it('panel + cross-doc ref → stay-in-context (no parent-doc navigation)', () => {
      const ref = makeRef({ document_id: 'doc-B', reference_id: 'ref-X' });
      const d = resolveReferenceNav({ ref, currentDocId: 'doc-A', stayInContext: false, refOpenMode: 'panel' });
      expect(d.kind).toBe('stay-in-context');
    });

    it('panel + same-doc ref → stay-in-context', () => {
      const ref = makeRef({ document_id: 'doc-A' });
      const d = resolveReferenceNav({ ref, currentDocId: 'doc-A', stayInContext: false, refOpenMode: 'panel' });
      expect(d.kind).toBe('stay-in-context');
    });

    it('panel + ancestor-owned ref → stay-in-context (the user stays in the child doc)', () => {
      const ref = makeRef({ document_id: 'doc-ancestor', reference_id: 'ref-anc' });
      const d = resolveReferenceNav({ ref, currentDocId: 'doc-child', stayInContext: false, refOpenMode: 'panel' });
      expect(d.kind).toBe('stay-in-context');
    });

    it('center + cross-doc ref still navigates (default mode unchanged)', () => {
      const ref = makeRef({ document_id: 'doc-B' });
      expect(resolveReferenceNav({ ref, currentDocId: 'doc-A', stayInContext: false, refOpenMode: 'center' }).kind).toBe('cross-doc');
    });

    it('split + cross-doc ref still navigates (split is a scope mode)', () => {
      const ref = makeRef({ document_id: 'doc-B' });
      expect(resolveReferenceNav({ ref, currentDocId: 'doc-A', stayInContext: false, refOpenMode: 'split' }).kind).toBe('cross-doc');
    });

    it('omitted refOpenMode defaults to center (existing callers unchanged)', () => {
      const ref = makeRef({ document_id: 'doc-B' });
      expect(resolveReferenceNav({ ref, currentDocId: 'doc-A', stayInContext: false }).kind).toBe('cross-doc');
    });
  });
});
