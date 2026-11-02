/** Page-lock grace window (SYSTEM: page-lock).
 * TEST-WHY: security-gate projection — guards the INVARIANT(security) at page-lock.ts
 * ("the open page is gated by the OUTERMOST locked, not-PIN-opened page on its parent
 * chain"): the grace window may widen "opened" in TIME (from the first leave, Presence
 * deletable) but never in SCOPE — a lapsed, zero, or absent (login payload) window must
 * close the page exactly like the pre-grace id-drop did. Fake timers own Date.now():
 * stamps and the gate are pure wall-clock projections. */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { selectOpenPageLockedBy } from './page-lock';
import { useAppStore } from '../store/app-store';
import type { Document, User } from '../types';

const T0 = 1_700_000_000_000;

function makeDoc(overrides: Partial<Document> & { document_id: string }): Document {
  return {
    project_id: 'proj1',
    parent_id: null,
    title: overrides.document_id,
    content: '',
    path: '',
    is_index: false,
    updated_at: '2025-01-01T00:00:00Z',
    created_at: '2025-01-01T00:00:00Z',
    ...overrides,
  };
}

function makeUser(overrides: Partial<User> = {}): User {
  return {
    user_id: 'u1',
    name: 'U1',
    email: 'u1@example.com',
    role: 'user',
    has_pin: true,
    user_facts: '',
    ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
});

afterEach(() => {
  useAppStore.setState({ currentUser: null, unlockedDocIds: [], pageLockLeftAt: {} });
  vi.useRealTimers();
});

describe('gate predicate — grace widens time, never scope', () => {
  // Locked parent L, open page is its child C: L is the outermost locked ancestor.
  const docs = [makeDoc({ document_id: 'L' }), makeDoc({ document_id: 'C', parent_id: 'L' })];
  const state = (graceS: number | null, leftAt: number | undefined) => {
    const pageLockLeftAt: Record<string, number> = {};
    if (leftAt !== undefined) pageLockLeftAt.L = leftAt;
    return {
      currentDocument: docs[1],
      currentUser: makeUser({
        locked_doc_ids: ['L'],
        ...(graceS !== null ? { page_lock_grace_s: graceS } : {}),
      }),
      unlockedDocIds: ['L'],
      pageLockLeftAt,
      documents: docs,
    };
  };

  // The grace window widens "opened" in TIME only: no stamp stays open, a stamp within
  // graceS renders open, at/past the boundary (strict <) the outermost locked ancestor
  // 'L' gates the child again.
  // TEST-WHY: pure-logic-edge — gate predicate widens opened in time, never in scope
  it('no stamp (never left) stays open; within grace renders open; at/past the boundary shows the PIN', () => {
    expect(selectOpenPageLockedBy(state(120, undefined))).toBe(null);
    vi.setSystemTime(T0 + 119_999);
    expect(selectOpenPageLockedBy(state(120, T0))).toBe(null);
    vi.setSystemTime(T0 + 120_000);
    expect(selectOpenPageLockedBy(state(120, T0))).toBe('L');
    vi.setSystemTime(T0 + 120_001);
    expect(selectOpenPageLockedBy(state(120, T0))).toBe('L');
  });

  // Fail-safe: an absent grace (login payload carries no field) or 0 (feature off) must
  // close any stamped id exactly like the pre-grace id-drop did.
  // TEST-WHY: pure-logic-edge — absent or zero grace closes any stamped id
  it('graceS null (login payload) or 0 closes any stamped id — fail-safe PIN', () => {
    vi.setSystemTime(T0 + 1);
    expect(selectOpenPageLockedBy(state(null, T0))).toBe('L');
    expect(selectOpenPageLockedBy(state(0, T0))).toBe('L');
  });
});

describe('stamp lifecycle — leave stamps once, presence deletes', () => {
  beforeEach(() => {
    useAppStore.setState({
      currentUser: makeUser({ locked_doc_ids: ['a'], page_lock_grace_s: 120 }),
      unlockedDocIds: ['a'],
      pageLockLeftAt: {},
    });
  });

  // The window counts from the FIRST leave (a later leave does not freshen), and
  // presence deletes (clearPageLockLeft; PIN success via addUnlockedDocId clears too,
  // or a re-PIN after a lapsed window could never open).
  // TEST-WHY: pure-logic-edge — stamp once, presence and PIN success delete
  it('a leave stamps once (no freshen); presence and PIN success delete the stamp', () => {
    useAppStore.getState().stampPageLockLeft(['a']);
    expect(useAppStore.getState().pageLockLeftAt).toEqual({ a: T0 });
    vi.setSystemTime(T0 + 60_000);
    useAppStore.getState().stampPageLockLeft(['a']);
    expect(useAppStore.getState().pageLockLeftAt).toEqual({ a: T0 });
    vi.setSystemTime(T0 + 30_000);
    useAppStore.getState().clearPageLockLeft(['a']);
    expect(useAppStore.getState().pageLockLeftAt).toEqual({});
    // Left again, then re-PIN after a lapsed window: the stamp must not survive a
    // successful PIN (the id stays held; the page must open).
    useAppStore.getState().stampPageLockLeft(['a']);
    useAppStore.getState().addUnlockedDocId('a');
    expect(useAppStore.getState().pageLockLeftAt).toEqual({});
    expect(useAppStore.getState().unlockedDocIds).toEqual(['a']);
  });
});
