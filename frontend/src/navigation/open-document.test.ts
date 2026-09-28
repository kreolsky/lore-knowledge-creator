/** openDocument — the ONE owner of commit-then-navigate (SYSTEM: document-navigation).
 *
 * Binds the INVARIANT of lessons/2026-09-17-guard-on-a-shared-commit-path-needs
 * -every-writer-driven.md: the target document is committed BEFORE navigate, for
 * every writer — the bus handler and the cross-doc ref branch alike.
 *
 * Default = open the DOCUMENT BODY (focusDocument clears the target's remembered
 * pointer first); `restore: true` (the tree only) skips focusDocument so the
 * remembered reference survives to setCurrentDocument's restoreFlow.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';

const { getStateMock } = vi.hoisted(() => ({ getStateMock: vi.fn() }));

vi.mock('../store/app-store', () => ({
  useAppStore: { getState: getStateMock },
}));

import { openDocument } from './open-document';

const DOC = { document_id: 'd1', title: 'Doc 1' };
const OTHER = { document_id: 'd2', title: 'Doc 2' };

function setup(overrides: Record<string, unknown> = {}) {
  const order: string[] = [];
  const setCurrentDocument = vi.fn(() => { order.push('commit'); });
  const focusDocument = vi.fn(() => { order.push('focus'); return 'navigate'; });
  const navigate = vi.fn(() => { order.push('navigate'); });
  getStateMock.mockReturnValue({
    documents: [DOC, OTHER],
    currentProject: { project_id: 'p1' },
    setCurrentDocument,
    focusDocument,
    ...overrides,
  });
  return { order, setCurrentDocument, focusDocument, navigate };
}

describe('openDocument', () => {
  beforeEach(() => { getStateMock.mockReset(); });

  it('unknown id → no-op: no focus, no commit, no navigate', () => {
    const { setCurrentDocument, focusDocument, navigate } = setup();
    openDocument('missing', { navigate });
    expect(setCurrentDocument).not.toHaveBeenCalled();
    expect(focusDocument).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("default (no restore), other doc → focusDocument (the pointer-clear owner) THEN commit THEN navigate", () => {
    const { order, focusDocument, setCurrentDocument, navigate } = setup();
    openDocument('d2', { navigate });
    expect(focusDocument).toHaveBeenCalledWith('d2');
    expect(setCurrentDocument).toHaveBeenCalledWith(OTHER);
    expect(navigate).toHaveBeenCalledWith('/docs/d2');
    expect(order).toEqual(['focus', 'commit', 'navigate']);
  });

  it("default + focusDocument 'stayed' (same doc, a reference/table focused) → no commit, no navigate (URL already the doc's)", () => {
    const stayedFocus = vi.fn(() => 'stayed');
    const { setCurrentDocument, navigate } = setup({ focusDocument: stayedFocus });
    openDocument('d1', { navigate });
    expect(stayedFocus).toHaveBeenCalledWith('d1');
    expect(setCurrentDocument).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("default + focusDocument 'navigate' (same doc, nothing focused) → normal path", () => {
    const { order, setCurrentDocument, navigate } = setup();
    openDocument('d1', { navigate });
    expect(setCurrentDocument).toHaveBeenCalledWith(DOC);
    expect(navigate).toHaveBeenCalledWith('/docs/d1');
    expect(order).toEqual(['focus', 'commit', 'navigate']);
  });

  it('restore: true (the tree) → focusDocument NOT called (remembered pointer intact), commit THEN navigate', () => {
    const { order, focusDocument, setCurrentDocument, navigate } = setup();
    openDocument('d2', { restore: true, navigate });
    expect(focusDocument).not.toHaveBeenCalled();
    expect(setCurrentDocument).toHaveBeenCalledWith(OTHER);
    expect(navigate).toHaveBeenCalledWith('/docs/d2');
    expect(order).toEqual(['commit', 'navigate']);
  });

  it('default with a pendingReference for the target (useEditorEvents cross-doc shape) → still focuses + commits; the pending ref wins the commit (applyRef pinned store-side)', () => {
    const { order, focusDocument, setCurrentDocument, navigate } = setup({
      pendingReference: { reference_id: 'r9', document_id: 'd2' },
    });
    openDocument('d2', { navigate });
    expect(focusDocument).toHaveBeenCalledWith('d2');
    expect(setCurrentDocument).toHaveBeenCalledWith(OTHER);
    expect(order).toEqual(['focus', 'commit', 'navigate']);
  });

  it('cross-doc ref path (injected navigate — useEditorEvents call shape) commits the TARGET doc before navigate', () => {
    // lessons/2026-09-17-guard-on-a-shared-commit-path-needs-every-writer-driven.md:
    // the cross-doc ref branch must reach the screen-swap through the SAME
    // commit-then-navigate owner, or the URL changes and the previous document
    // stays on screen.
    const { order, setCurrentDocument, navigate } = setup();
    openDocument('d2', { navigate });
    expect(setCurrentDocument).toHaveBeenCalledWith(OTHER);
    expect(order.indexOf('commit')).toBeLessThan(order.indexOf('navigate'));
    expect(navigate).toHaveBeenCalledWith('/docs/d2');
  });

  it('no currentProject → still focuses and commits, never navigates (Header parity)', () => {
    const { order, setCurrentDocument, navigate } = setup({ currentProject: null });
    openDocument('d1', { navigate });
    expect(setCurrentDocument).toHaveBeenCalledWith(DOC);
    expect(navigate).not.toHaveBeenCalled();
    expect(order).toEqual(['focus', 'commit']);
  });
});
