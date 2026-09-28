/**
 * Unit tests for the pure share-state derivation + toggle planner.
 *
 * The reducer is the single place the two-checkbox semantics live; the AccessPanel
 * component just renders the derived state and executes the planned actions. Pinned
 * here so the wiring test (AccessPanel.test.tsx) can stay a smoke tier.
 */
import { describe, it, expect } from 'vitest';
import { deriveShareState, planShareToggle, type ShareRow } from './share-state';

function row(id: string, scope: 'doc' | 'subtree' = 'doc'): ShareRow {
  return { share_id: id, scope };
}

describe('deriveShareState', () => {
  it('no rows, no inheritance → not-shared', () => {
    const s = deriveShareState([], null);
    expect(s.summary).toBe('not-shared');
    expect(s.shared).toBe(false);
    expect(s.subtree).toBe(false);
  });

  it('one doc row → doc summary, shared on, subtree off', () => {
    const s = deriveShareState([row('a', 'doc')], null);
    expect(s.summary).toBe('doc');
    expect(s.shared).toBe(true);
    expect(s.subtree).toBe(false);
  });

  it('a subtree row → subtree summary (shared + subtree both on)', () => {
    const s = deriveShareState([row('a', 'subtree')], null);
    expect(s.summary).toBe('subtree');
    expect(s.shared).toBe(true);
    expect(s.subtree).toBe(true);
  });

  it('legacy two-row input derives ONE coherent state (subtree wins the summary)', () => {
    // Legacy rows predating this change may be several; they collapse to one state.
    const s = deriveShareState([row('a', 'doc'), row('b', 'subtree')], null);
    expect(s.shared).toBe(true);
    expect(s.subtree).toBe(true);
    expect(s.summary).toBe('subtree');
  });

  it('no own row + inherited_from → summary inherited, NEVER not-shared', () => {
    const s = deriveShareState([], { document_id: 'parent', title: 'Folder' });
    expect(s.summary).toBe('inherited');
    expect(s.shared).toBe(false);
    expect(s.subtree).toBe(false);
  });

  it('own row wins over inheritance (inherited_from ignored when an own row exists)', () => {
    const s = deriveShareState([row('a', 'doc')], { document_id: 'parent', title: 'Folder' });
    expect(s.summary).toBe('doc');
  });

  it('subtree checkbox is disabled only when neither shared nor inherited', () => {
    expect(deriveShareState([], null).subtreeDisabled).toBe(true);
    expect(deriveShareState([row('a')], null).subtreeDisabled).toBe(false); // shared
    expect(deriveShareState([], { document_id: 'p', title: 'P' }).subtreeDisabled).toBe(false); // inherited keeps it enabled
  });
});

describe('planShareToggle', () => {
  it('turning Share OFF revokes EVERY row (one click fully unshares)', () => {
    const state = deriveShareState([row('a'), row('b', 'subtree')], null);
    const plan = planShareToggle(state, 'shared', [row('a'), row('b', 'subtree')]);
    expect(plan.actions).toEqual([
      { kind: 'revoke', shareId: 'a' },
      { kind: 'revoke', shareId: 'b' },
    ]);
  });

  it('turning Share ON creates a doc share', () => {
    const state = deriveShareState([], null);
    const plan = planShareToggle(state, 'shared', []);
    expect(plan.actions).toEqual([{ kind: 'create', scope: 'doc' }]);
  });

  it('checking subtree with no own row CREATES a subtree share (turns both on)', () => {
    // inherited state: checkboxes off+enabled; ticking subtree mints an own subtree root.
    const state = deriveShareState([], { document_id: 'p', title: 'P' });
    const plan = planShareToggle(state, 'subtree', []);
    expect(plan.actions).toEqual([{ kind: 'create', scope: 'subtree' }]);
  });

  it('checking subtree when shared (own doc row) PATCHES that row to subtree', () => {
    const state = deriveShareState([row('a', 'doc')], null);
    const plan = planShareToggle(state, 'subtree', [row('a', 'doc')]);
    expect(plan.actions).toEqual([{ kind: 'patch', shareId: 'a', scope: 'subtree' }]);
  });

  it('unchecking subtree PATCHES the subtree row to doc (share stays on)', () => {
    const state = deriveShareState([row('a', 'subtree')], null);
    const plan = planShareToggle(state, 'subtree', [row('a', 'subtree')]);
    expect(plan.actions).toEqual([{ kind: 'patch', shareId: 'a', scope: 'doc' }]);
  });

  it('unchecking subtree narrows EVERY legacy subtree row in one toggle', () => {
    // Legacy docs can carry several subtree rows; narrowing only the first would leave
    // the checkbox stuck checked. All subtree rows → doc here.
    const shares = [row('a', 'subtree'), row('b', 'doc'), row('c', 'subtree')];
    const state = deriveShareState(shares, null);
    const plan = planShareToggle(state, 'subtree', shares);
    expect(plan.actions).toEqual([
      { kind: 'patch', shareId: 'a', scope: 'doc' },
      { kind: 'patch', shareId: 'c', scope: 'doc' },
    ]);
  });

  it('widen + narrow round-trip preserves the row id (no revoke-then-create churn)', () => {
    // Scope change is an atomic PATCH, keeping the row's audit trail + URL.
    const shares = [row('keep', 'doc')];
    const widen = planShareToggle(deriveShareState(shares, null), 'subtree', shares);
    expect(widen.actions).toEqual([{ kind: 'patch', shareId: 'keep', scope: 'subtree' }]);
    const narrowed = [row('keep', 'subtree')];
    const narrow = planShareToggle(deriveShareState(narrowed, null), 'subtree', narrowed);
    expect(narrow.actions).toEqual([{ kind: 'patch', shareId: 'keep', scope: 'doc' }]);
  });
});
