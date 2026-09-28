/**
 * Tests for the active-editor slots: focused vs root view, focused role/content type,
 * focused handle, role-keyed views, and the previous-match guard on every clear.
 *
 * The root/focused split exists because a focused table cell's nested EditorView must drive
 * the toolbar without becoming the content-capture source: a manual snapshot (Cmd+S) must
 * store the document, not the cell's text.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import * as Y from 'yjs';
import type { EditorView } from '@codemirror/view';
import {
  claimFocus,
  claimNestedView,
  mountView,
  unmountView,
  publishHandle,
  releaseHandle,
  getActiveHandle,
  getActiveTablesJson,
  getEditorView,
  getRootEditorView,
  getFocusedRole,
  getFocusedIsReference,
  useEditorView,
  useEditorContent,
  setRoleView,
  getRoleView,
  subscribeRoleView,
} from './active-editor';
import type { EntityYjsState } from '../collab/yjs-provider';
import { createTable, serializeTables } from '../components/editor/live-preview/table-block-model';

// Lightweight stand-ins for CM6 EditorView (the slots only store/return them).
function fakeView(tag: string, text = tag): EditorView {
  return { tag, state: { doc: { toString: () => text } } } as unknown as EditorView;
}
const host = fakeView('host');
const cell = fakeView('cell');
const hostB = fakeView('hostB');

function fakeHandle(ydoc: Y.Doc): EntityYjsState {
  return { ydoc } as unknown as EntityYjsState;
}

/** Clear every slot and restore the focus defaults. */
function resetSlots(): void {
  claimFocus({ role: 'primary', isReference: false, view: host, handle: null });
  unmountView();
  releaseHandle();
  setRoleView('primary', null);
  setRoleView('secondary', null);
}

describe('active-editor — root/focused split', () => {
  beforeEach(resetSlots);

  it('a cell focus moves the focused view, not the root', () => {
    claimFocus({ role: 'primary', isReference: false, view: host, handle: null });
    claimNestedView(cell);
    expect(getEditorView()).toBe(cell);       // toolbar follows the cell
    expect(getRootEditorView()).toBe(host);   // content source stays the host
    expect(useEditorContent()()).toBe('host');
  });

  it('claimFocus sets role, content type, both views and the handle', () => {
    const handle = fakeHandle(new Y.Doc());
    claimFocus({ role: 'secondary', isReference: true, view: hostB, handle });
    expect(getFocusedRole()).toBe('secondary');
    expect(getFocusedIsReference()).toBe(true);
    expect(getEditorView()).toBe(hostB);
    expect(getRootEditorView()).toBe(hostB);
    expect(getActiveHandle()).toBe(handle);
  });

  it('mountView points both views at the mounting view without changing the focused role', () => {
    claimFocus({ role: 'secondary', isReference: true, view: hostB, handle: null });
    mountView(host);
    expect(getEditorView()).toBe(host);
    expect(getRootEditorView()).toBe(host);
    expect(getFocusedRole()).toBe('secondary');
    expect(getFocusedIsReference()).toBe(true);
  });

  it('previous-match guard: unmounting a foreign view leaves the current one', () => {
    mountView(host);
    unmountView(hostB);
    expect(getEditorView()).toBe(host);
    expect(getRootEditorView()).toBe(host);
    unmountView(host);
    expect(getEditorView()).toBeNull();
    expect(getRootEditorView()).toBeNull();
  });

  it('useEditorView() still returns the primary view after a secondary unmount', () => {
    // A secondary column mounted, then focus returned to the primary; the secondary's
    // teardown clears with its own view as `previous` — React readers keep the primary.
    mountView(hostB);
    claimFocus({ role: 'primary', isReference: false, view: host, handle: null });
    unmountView(hostB);
    expect(useEditorView()()).toBe(host);
    expect(useEditorContent()()).toBe('host');
  });

  it('secondary unmount leaves the primary view, role view and handle intact', () => {
    const primaryHandle = fakeHandle(new Y.Doc());
    const secondaryHandle = fakeHandle(new Y.Doc());
    setRoleView('primary', host);
    setRoleView('secondary', hostB);
    claimFocus({ role: 'primary', isReference: false, view: host, handle: primaryHandle });

    // The secondary column's teardown: guarded view, role-view and handle clears.
    unmountView(hostB);
    setRoleView('secondary', null, hostB);
    releaseHandle(secondaryHandle);

    expect(getEditorView()).toBe(host);
    expect(getRootEditorView()).toBe(host);
    expect(getRoleView('primary')).toBe(host);
    expect(getRoleView('secondary')).toBeNull();
    expect(getActiveHandle()).toBe(primaryHandle);
  });
});

describe('active-editor — focused handle', () => {
  beforeEach(resetSlots);

  it('releaseHandle clears only while the slot still holds `previous`', () => {
    const mine = fakeHandle(new Y.Doc());
    const foreign = fakeHandle(new Y.Doc());
    publishHandle(mine);
    releaseHandle(foreign);
    expect(getActiveHandle()).toBe(mine);
    releaseHandle(mine);
    expect(getActiveHandle()).toBeNull();
  });

  it("getActiveTablesJson reads the focused column's handle", () => {
    const docA = new Y.Doc();
    const docB = new Y.Doc();
    createTable(docA, [['a']]);
    createTable(docB, [['b1', 'b2']]);
    claimFocus({ role: 'primary', isReference: false, view: host, handle: fakeHandle(docA) });
    claimFocus({ role: 'secondary', isReference: true, view: hostB, handle: fakeHandle(docB) });
    expect(getActiveTablesJson()).toBe(serializeTables(docB));
    expect(getActiveTablesJson()).not.toBe(serializeTables(docA));
  });

  it("getActiveTablesJson is '{}' with no handle", () => {
    expect(getActiveTablesJson()).toBe('{}');
  });
});

describe('active-editor — role-keyed view registry (split view)', () => {
  beforeEach(resetSlots);

  it('setRoleView(secondary) does NOT affect getRoleView(primary)', () => {
    setRoleView('primary', host);
    setRoleView('secondary', cell);
    expect(getRoleView('primary')).toBe(host);
    expect(getRoleView('secondary')).toBe(cell);
  });

  it('getRoleView returns null for an unset role', () => {
    setRoleView('primary', host);
    expect(getRoleView('secondary')).toBeNull();
  });

  it('overwrites the same role slot when set again', () => {
    setRoleView('secondary', cell);
    setRoleView('secondary', hostB);
    expect(getRoleView('secondary')).toBe(hostB);
  });

  it('clearing secondary with a matching previous leaves primary intact', () => {
    setRoleView('primary', host);
    setRoleView('secondary', cell);
    setRoleView('secondary', null, cell);
    expect(getRoleView('secondary')).toBeNull();
    expect(getRoleView('primary')).toBe(host);
  });

  it('previous-match guard: a primary unmount does NOT clear the secondary slot', () => {
    setRoleView('primary', host);
    setRoleView('secondary', cell);
    // A non-matching previous must be a no-op — the other role survives.
    setRoleView('secondary', null, hostB);
    expect(getRoleView('secondary')).toBe(cell);
  });

  it('subscribeRoleView fires on set and on clear', () => {
    const calls: (EditorView | null)[] = [];
    const off = subscribeRoleView((_role, v) => calls.push(v));

    setRoleView('primary', host);
    setRoleView('primary', null, host);

    expect(calls).toEqual([host, null]);
    off();
  });

  it('subscribeRoleView does not fire on a no-op clear (non-matching previous)', () => {
    setRoleView('secondary', cell);
    const calls: (EditorView | null)[] = [];
    const off = subscribeRoleView((_role, v) => calls.push(v));
    setRoleView('secondary', null, host); // non-matching → no-op
    expect(calls).toEqual([]);
    off();
  });
});
