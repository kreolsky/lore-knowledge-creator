/**
 * Unit tests for tableReconcileExtension — the debounced host-doc trigger that
 * clones duplicate anchors (short timer). Auto orphan-cleanup was REMOVED (a text
 * edit must never delete a table model — see the retain tests below).
 *
 * These tests pin the data-loss invariants:
 *  - a docChanged that removes OR mangles an anchor RETAINS the model (no cleanup);
 *  - the target doc captured at arm time MUST be re-verified at fire time (clone);
 *  - view destroy MUST cancel the timer.
 *
 * // @vitest-environment jsdom
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import * as Y from 'yjs';
import { EditorState, Prec } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { tableReconcileExtension } from './table-reconcile';
import { createTable, getTablesMap, tableAnchor, DEFAULT_COLUMN_WIDTH } from './table-block-model';
import { releaseHandle, publishHandle } from '../../../editor/active-editor';
import { DOC } from '../../../collab/entity-types';
import type { EntityYjsState } from '../../../collab/yjs-provider';

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Build a minimal EntityYjsState stub bound to a fresh Y.Doc. */
function makeHandle(opts: { synced?: boolean } = {}): EntityYjsState {
  const ydoc = new Y.Doc();
  return {
    entityType: DOC,
    ydoc,
    ytext: ydoc.getText('content'),
    awareness: {} as EntityYjsState['awareness'],
    callbackBundles: [],
    local: null,
    localLoaded: true,
    hasUnsyncedLocalEdits: false,
    unsentReported: false,
    synced: opts.synced ?? true,
    refCount: 1,
    leave: () => {},
    disposeObserver: () => {},
    disposeAwareness: () => {},
  };
}

/** Seed a table model under a literal id (for clone tests / duplicate anchors). */
function seedLiteralTable(doc: Y.Doc, id: string, label = 'a') {
  doc.transact(() => {
    const tables = doc.getMap('tables');
    const table = new Y.Map();
    const col = () => { const m = new Y.Map<number>(); m.set('w', DEFAULT_COLUMN_WIDTH); return m; };
    const cols = new Y.Array(); cols.push([col()]);
    table.set('columns', cols);
    const cell = () => { const c = new Y.Map<Y.Text>(); c.set('t', new Y.Text(label)); return c; };
    const row = new Y.Array(); row.push([cell()]);
    const rows = new Y.Array(); rows.push([row]);
    table.set('rows', rows);
    tables.set(id, table);
  });
}

function setContent(handle: EntityYjsState, text: string) {
  const yt = handle.ytext;
  yt.delete(0, yt.length);
  yt.insert(0, text);
}

function mountView(content: string, enabled: () => boolean = () => true): EditorView {
  const state = EditorState.create({
    doc: content,
    extensions: [tableReconcileExtension(enabled)],
  });
  return new EditorView({ state, parent: document.createElement('div') });
}

/** Append text to a view to arm the reconcile timers. */
function arm(view: EditorView, text = 'x') {
  view.dispatch({ changes: { from: view.state.doc.length, insert: text } });
}

/**
 * Arm the timers while keeping the view and the handle's ytext in lock-step —
 * what yCollab does in the live app (a doc edit flows into both). The clone path
 * only fires when the view text equals the ydoc content, so tests that exercise it
 * must keep them synced.
 */
function armSynced(view: EditorView, handle: EntityYjsState, text = 'x') {
  const at = view.state.doc.length;
  view.dispatch({ changes: { from: at, insert: text } });
  handle.ytext.insert(at, text);
}

beforeEach(() => {
  releaseHandle();
});

afterEach(() => {
  vi.useRealTimers();
  releaseHandle();
});

// ── Data-loss guard: a text edit NEVER deletes a table model ──────────────────

describe('anchor removal / corruption retains the model (no auto-cleanup)', () => {
  beforeEach(() => vi.useFakeTimers());

  it('deleting the anchor line RETAINS the model (deletion must be explicit UI)', () => {
    const handle = makeHandle();
    const id = createTable(handle.ydoc, [['a', 'b'], ['c', 'd']]);
    const anchor = tableAnchor('T', id);
    const content = `lead\n${anchor}\ntrail`;
    setContent(handle, content);
    publishHandle(handle);

    const view = mountView(content);
    const newText = 'lead\ntrail';
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: newText } });
    setContent(handle, newText);

    vi.advanceTimersByTime(5000);
    // The model survives — only an explicit UI delete may drop it.
    expect(getTablesMap(handle.ydoc).has(id)).toBe(true);
  });

  it('mangling a single char in the anchor id RETAINS the model (no silent whole-table loss)', () => {
    const handle = makeHandle();
    const id = createTable(handle.ydoc, [['a']]);
    const anchor = tableAnchor('T', id);
    const content = `lead ${anchor} trail`;
    setContent(handle, content);
    publishHandle(handle);

    const view = mountView(content);
    // Corrupt the id (drop one char) — the anchor no longer matches the model.
    const mangled = content.replace(`table:${id}`, `table:${id.slice(1)}`);
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: mangled } });
    setContent(handle, mangled);

    vi.advanceTimersByTime(5000);
    expect(getTablesMap(handle.ydoc).has(id)).toBe(true);
  });

  it('SHORT timer clones a pasted duplicate anchor under a fresh id', () => {
    const id = 'src';
    const handle = makeHandle();
    seedLiteralTable(handle.ydoc, id);
    const dup = `${tableAnchor('T', id)} ${tableAnchor('T', id)}`;
    setContent(handle, dup);
    publishHandle(handle);

    const view = mountView(dup);
    armSynced(view, handle);

    vi.advanceTimersByTime(300);
    expect([...getTablesMap(handle.ydoc).keys()]).toHaveLength(2);
    // The view was rewritten with two distinct ids.
    const viewIds = view.state.doc.toString().match(/table:([^\)]+)/g)!;
    expect(new Set(viewIds).size).toBe(2);
  });
});

// ── enabled() gate ───────────────────────────────────────────────────────────

describe('enabled() === false', () => {
  beforeEach(() => vi.useFakeTimers());

  it('does not run orphan cleanup', () => {
    const handle = makeHandle();
    const id = createTable(handle.ydoc, [['a']]);
    publishHandle(handle);
    const view = mountView('plain text, no anchor', () => false);
    arm(view);
    vi.advanceTimersByTime(3000);
    expect(getTablesMap(handle.ydoc).has(id)).toBe(true);
  });

  it('does not clone (no buffer rewrite, no new model)', () => {
    const id = 'dup-src';
    const handle = makeHandle();
    seedLiteralTable(handle.ydoc, id);
    const dup = `${tableAnchor('T', id)} ${tableAnchor('T', id)}`;
    setContent(handle, dup);
    publishHandle(handle);
    const view = mountView(dup, () => false);
    arm(view);
    vi.advanceTimersByTime(300);
    expect([...getTablesMap(handle.ydoc).keys()]).toHaveLength(1);
    expect(view.state.doc.toString()).toBe(dup + 'x');
  });
});

// ── Vector 2: handle swapped between arm and fire ────────────────────────────

describe('doc switch between arm and fire', () => {
  beforeEach(() => vi.useFakeTimers());

  it('does NOT delete models in the new doc (identity check)', () => {
    const handleA = makeHandle();
    const idA = createTable(handleA.ydoc, [['a']]);
    setContent(handleA, `lead\n${tableAnchor('T', idA)}\ntrail`);

    const handleB = makeHandle();
    const idB = createTable(handleB.ydoc, [['b']]);
    setContent(handleB, `other\n${tableAnchor('T', idB)}\nmore`);

    publishHandle(handleA);
    const view = mountView(handleA.ytext.toString());
    arm(view);

    // Switch active handle to doc B BEFORE the timers fire.
    publishHandle(handleB);
    vi.advanceTimersByTime(3000);

    expect(getTablesMap(handleB.ydoc).has(idB)).toBe(true);
    expect(getTablesMap(handleA.ydoc).has(idA)).toBe(true);
  });
});

// ── Vector: view.destroy() cancels timers ────────────────────────────────────

describe('view teardown', () => {
  beforeEach(() => vi.useFakeTimers());

  it('destroy() before the 3s deadline → no deletion', () => {
    const handle = makeHandle();
    const id = createTable(handle.ydoc, [['a']]);
    publishHandle(handle);
    const view = mountView('text without the anchor');
    arm(view);

    view.destroy();
    vi.advanceTimersByTime(3000);
    expect(getTablesMap(handle.ydoc).has(id)).toBe(true);
  });
});

// ── Vector 3: reconnect window (stale/derived view text vs live ydoc) ────────

describe('reconnect window — view holds derived anchor-less text', () => {
  beforeEach(() => vi.useFakeTimers());

  it('cleanup reads the Y.Doc content, so the model survives', () => {
    const handle = makeHandle();
    const id = createTable(handle.ydoc, [['a']]);
    setContent(handle, tableAnchor('T', id));
    publishHandle(handle);

    // View seeded with derived (anchor-less) GFM text — simulates a reconnect
    // remount before the yjs sync swaps in the real ytext.
    const view = mountView('| col |\n| --- |\n| a |\n');
    arm(view);

    vi.advanceTimersByTime(3000);
    expect(getTablesMap(handle.ydoc).has(id)).toBe(true);
  });

  it('clone does not rewrite a stale view whose text != ydoc content', () => {
    const id = 'src';
    const handle = makeHandle();
    seedLiteralTable(handle.ydoc, id);
    setContent(handle, `${tableAnchor('T', id)} ${tableAnchor('T', id)}`);
    publishHandle(handle);

    const stale = 'totally different derived text';
    const view = mountView(stale);
    arm(view);
    vi.advanceTimersByTime(300);
    expect([...getTablesMap(handle.ydoc).keys()]).toHaveLength(1);
    // The stale buffer is untouched by clone (the 'x' is just the arm append).
    expect(view.state.doc.toString()).toBe(stale + 'x');
  });
});

// ── Snapshot preview is a pure viewer (read-only facet precedence) ───────────

describe('snapshot preview read-only facet', () => {
  it('EditorView.editable facet is false when the read-only pair overrides the editable default', () => {
    // Mirrors Editor.tsx: the shared cmExtensions defaults editable to true; the
    // preview appends the read-only pair at highest precedence so it overrides that
    // default (a plain editable.of(false) loses on equal precedence).
    const shared: import('@codemirror/state').Extension[] = [
      EditorView.editable.of(true),
      tableReconcileExtension(() => false),
    ];
    const previewExts = [
      ...shared,
      Prec.highest(EditorState.readOnly.of(true)),
      Prec.highest(EditorView.editable.of(false)),
    ];
    const state = EditorState.create({ doc: 'snapshot text', extensions: previewExts });
    const view = new EditorView({ state, parent: document.createElement('div') });
    expect(view.state.facet(EditorView.editable)).toBe(false);
    expect(state.readOnly).toBe(true);
    view.destroy();
  });
});
