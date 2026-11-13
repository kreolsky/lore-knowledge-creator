/**
 * Tests for TableBlockWidget mounted in a real document EditorView: column widths
 * (0 = auto, px = fixed), the selection outline and read-only hosts.
 */
// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import * as Y from 'yjs';
import type { EditorView } from '@codemirror/view';
import { Compartment, EditorState } from '@codemirror/state';
import { EditorView as EV } from '@codemirror/view';
import { createTable, readTableModel, readTableShape, TABLE_LOCAL_ORIGIN } from './table-block-model';
import { tableAnchor } from './table-document-ops';
import { createDocumentUndo } from '../../../collab/document-undo';
import { createYjsExtension } from '../../../collab/yjs-binding';
import { REPLACE_ALL_ORIGIN } from '../find-replace-undo';
import { editorExperience, renderExtensions } from '../render-bundle';
import { tableDocSource } from './table-doc-source';
import { LINK_TYPES, type LinkKind } from './link-types';
import { renderCellStatic } from './cell-static-render';

vi.mock('mermaid', () => ({
  default: { initialize: vi.fn(), render: vi.fn(async () => ({ svg: '<svg></svg>' })) },
}));

// ── Full-mount harness (facet-supplied model, like table-block-deferred-render) ──
// The widths/selection behavior needs the widget's real DOM inside a document
// EditorView; the model is supplied through tableDocSource (no live handle needed).

const mounted: { view: EditorView; parent: HTMLElement }[] = [];

const editableSlot = new Compartment();

function mountTable(opts?: { readOnly?: boolean; commentator?: boolean }): { view: EditorView; parent: HTMLElement; doc: Y.Doc; id: string } {
  const ydoc = new Y.Doc();
  const id = createTable(ydoc, [['a', 'b'], ['c', 'd']]);
  const parent = document.createElement('div');
  document.body.appendChild(parent);
  const view = new EV({
    parent,
    state: EditorState.create({
      doc: `x\n${tableAnchor('T', id)}\ny`,
      extensions: [
        editorExperience({ mode: 'document' }),
        renderExtensions(),
        tableDocSource.of(ydoc),
        ...(opts?.readOnly
          ? [EditorState.readOnly.of(true), EV.editable.of(false)]
          : []),
        editableSlot.of(EV.editable.of(!opts?.commentator)),
      ],
    }),
  });
  mounted.push({ view, parent });
  return { view, parent, doc: ydoc, id };
}

// jsdom has no layout and no elementFromPoint; the cell mount path asks for it.
beforeEach(() => { document.elementFromPoint = () => null; });

afterEach(() => {
  delete (document as { elementFromPoint?: unknown }).elementFromPoint;
  while (mounted.length) {
    const m = mounted.pop()!;
    m.view.destroy();
  }
  document.body.replaceChildren();
});

describe('TableBlockWidget — selection outline + read-only hosts', () => {
  it('selecting the anchor line outlines the wrap (ViewPlugin class)', () => {
    const { view, parent, id } = mountTable();
    const wrap = parent.querySelector('[data-table-id]') as HTMLElement;
    expect(wrap.classList.contains('cm-table-block-wrap--selected')).toBe(false);
    const at = view.state.doc.toString().indexOf(tableAnchor('T', id));
    const line = view.state.doc.lineAt(at);
    view.dispatch({ selection: { anchor: line.from, head: line.to } });
    expect(wrap.classList.contains('cm-table-block-wrap--selected')).toBe(true);
  });
});

// A full click: a plain press focuses its cell only on the release inside that cell,
// which the widget resolves through elementFromPoint — aimed at `el` for the mouseup.
const press = (el: Element) => {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
  document.elementFromPoint = () => el;
  try { el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true })); }
  finally { document.elementFromPoint = () => null; }
};
const firstCell = (parent: HTMLElement) => parent.querySelector('td') as HTMLElement;
const cellEditors = (parent: HTMLElement) => parent.querySelectorAll('.cm-table-block-cell-editor .cm-editor');

describe('TableBlockWidget — a host that is not editable', () => {
  // TEST-WHY: refused-principal — a commentator's host is editable-false but not readOnly, the principal that must be refused a cell editor and the cell menu.
  it('a commentator is denied the cell editor and the cell menu', () => {
    const { parent, doc, id } = mountTable({ commentator: true });
    press(firstCell(parent));
    expect(cellEditors(parent)).toHaveLength(0);
    const menu = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
    firstCell(parent).dispatchEvent(menu);
    expect(menu.defaultPrevented).toBe(false);
    expect(document.querySelector('[role="menu"]')).toBeNull();
    expect(readTableShape(doc, id)).toEqual({ rows: 2, cols: 2 });
  });

  // TEST-WHY: refused-principal — a commentator must never enter cell selection mode, whose Delete would clear cells the server then drops.
  it('a commentator is denied cell selection: no range, no tabindex, Delete changes no cell', () => {
    const { parent, doc, id } = mountTable({ commentator: true });
    const wrap = parent.querySelector('.cm-table-block-wrap') as HTMLElement;
    firstCell(parent).dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, shiftKey: true }));
    for (const key of ['Escape', 'Delete']) {
      wrap.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
    }
    expect(parent.querySelector('.cm-table-cell-selected')).toBeNull();
    expect(wrap.hasAttribute('tabindex')).toBe(false);
    expect(readTableModel(doc, id)?.rows).toEqual([['a', 'b'], ['c', 'd']]);
  });

  // TEST-WHY: refused-principal — access dropping while a cell is open must leave no live editor, since the eq() widget is never rebuilt.
  it('a mounted cell editor is denied once editability is lost', async () => {
    const { view, parent } = mountTable();
    press(firstCell(parent));
    expect(cellEditors(parent)).toHaveLength(1);
    view.dispatch({ effects: editableSlot.reconfigure(EV.editable.of(false)) });
    await Promise.resolve();
    expect(cellEditors(parent)).toHaveLength(0);
  });
});

describe('TableBlockWidget — Backspace row delete', () => {
  const backspace = (cell: EditorView) => cell.contentDOM.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'Backspace', bubbles: true, cancelable: true }));
  const mountCell = (parent: HTMLElement, el: Element) => {
    press(el);
    return EV.findFromDOM(el.querySelector('.cm-editor') as HTMLElement)!;
  };

  // TEST-WHY: data-loss — Backspace must never delete the header row or a row holding text in a non-first cell; only an all-empty body row may go.
  it('refuses the header row and a row with text elsewhere', () => {
    const { parent, doc, id } = mountTable();
    const head = mountCell(parent, parent.querySelector('th')!);
    head.dispatch({ changes: { from: 0, to: head.state.doc.length, insert: '' } });
    head.dispatch({ selection: { anchor: 0 } });
    backspace(head);
    expect(readTableShape(doc, id)).toEqual({ rows: 2, cols: 2 });
    const body = mountCell(parent, firstCell(parent));
    body.dispatch({ changes: { from: 0, to: body.state.doc.length, insert: '' } });
    body.dispatch({ selection: { anchor: 0 } });
    backspace(body); // the second cell still holds 'd'
    expect(readTableShape(doc, id)).toEqual({ rows: 2, cols: 2 });
    const second = mountCell(parent, parent.querySelectorAll('td')[1]);
    second.dispatch({ changes: { from: 0, to: second.state.doc.length, insert: '' } });
    const emptied = mountCell(parent, firstCell(parent));
    emptied.dispatch({ selection: { anchor: 0 } });
    backspace(emptied); // now every cell of the body row is empty
    expect(readTableShape(doc, id)).toEqual({ rows: 1, cols: 2 });
  });
});

describe('TableBlockWidget — Ctrl+Z in a cell', () => {
  // TEST-WHY: pure-logic-edge — the one-press-one-step INVARIANT in yjs-binding: the cell editor lives inside the host's content and both bind the SAME stack, so a single Mod-z keydown in the cell must pop exactly one item, not one per editor.
  it('one Mod-z keydown in a mounted cell pops exactly one stack item', () => {
    const ydoc = new Y.Doc();
    const id = createTable(ydoc, [['a', 'b'], ['c', 'd']]);
    const text = `x\n${tableAnchor('T', id)}\ny`;
    ydoc.getText('content').insert(0, text);
    const manager = createDocumentUndo(ydoc, [TABLE_LOCAL_ORIGIN, REPLACE_ALL_ORIGIN]);
    const parent = document.body.appendChild(document.createElement('div'));
    const view = new EV({
      parent,
      state: EditorState.create({
        doc: text,
        extensions: [editorExperience({ mode: 'document' }), renderExtensions(), tableDocSource.of(ydoc),
          createYjsExtension(ydoc.getText('content'), manager)],
      }),
    });
    mounted.push({ view, parent });
    view.dispatch({ changes: { from: 0, insert: 'H' } });
    manager.stopCapturing();
    press(firstCell(parent));
    const cell = EV.findFromDOM(cellEditors(parent)[0] as HTMLElement)!;
    cell.dispatch({ changes: { from: 1, insert: '1' } });
    manager.stopCapturing();
    cell.dispatch({ changes: { from: 2, insert: '2' } });
    expect(manager.undoStack).toHaveLength(3);
    cell.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: 'z', ctrlKey: true, bubbles: true, cancelable: true }));
    expect(manager.undoStack).toHaveLength(2);
    expect(cell.state.doc.toString()).toBe('c1');
    expect(view.state.doc.toString().startsWith('Hx')).toBe(true);
  });
});

// ── Link parity: host text vs mounted cell editor vs static placeholder ──
// One sample per registry kind; the Record forces a sample for every LinkKind, and the cases
// below walk LINK_TYPES, so a new link type joins the proof by existing.
const LINK_SAMPLES: Record<LinkKind, string> = {
  ref: '[pic](ref:r1)',
  doc: '[page](d1)',
  ext: '[site](https://example.com/a)',
  bare: 'see https://example.com/b now',
};
const LINK_CLASSES = [...new Set(LINK_TYPES.map((e) => e.decorationClass))];
const linkMarks = (root: ParentNode): string[] =>
  [...root.querySelectorAll('*')]
    .flatMap((el) => [...el.classList])
    .filter((c) => LINK_CLASSES.some((base) => c === base || c === `${base}-broken`))
    .sort();

function mountWithCellText(sample: string): { view: EditorView; parent: HTMLElement } {
  const ydoc = new Y.Doc();
  const id = createTable(ydoc, [['h', 'b'], [sample, 'd']]); // firstCell = first td = first body row
  const parent = document.createElement('div');
  document.body.appendChild(parent);
  const view = new EV({
    parent,
    state: EditorState.create({
      doc: `x\n${tableAnchor('T', id)}\n${sample}\ny`,
      extensions: [editorExperience({ mode: 'document' }), renderExtensions(), tableDocSource.of(ydoc)],
    }),
  });
  mounted.push({ view, parent });
  return { view, parent };
}

describe('TableBlockWidget — link marks are identical in host text and in a cell', () => {
  // TEST-WHY: cross-process-contract — a cell renders links through its own editor and a static placeholder, two renderers that must match the host for every LINK_TYPES kind.
  it.each(LINK_TYPES.map((e) => [e.kind, LINK_SAMPLES[e.kind]] as const))('%s', (_kind, sample) => {
    const { view, parent } = mountWithCellText(sample);
    const sampleLine = view.state.doc.line(view.state.doc.lines - 1);
    const hostLine = view.domAtPos(sampleLine.from + 1).node.parentElement!.closest('.cm-line')!;
    const host = linkMarks(hostLine);
    expect(host.length).toBeGreaterThan(0);
    expect(linkMarks(renderCellStatic(sample) as ParentNode)).toEqual(host);
    press(firstCell(parent));
    const cell = parent.querySelector('.cm-table-block-cell-editor .cm-editor .cm-content')!;
    expect(linkMarks(cell)).toEqual(host);
  });
});
