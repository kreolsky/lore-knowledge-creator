/**
 * Tests for TableBlockWidget mounted in a real document EditorView: column widths
 * (0 = auto, px = fixed), the selection outline and read-only hosts.
 */
// @vitest-environment jsdom
import { describe, it, expect, afterEach, vi } from 'vitest';
import * as Y from 'yjs';
import type { EditorView } from '@codemirror/view';
import { Compartment, EditorState } from '@codemirror/state';
import { EditorView as EV } from '@codemirror/view';
import { createTable, tableAnchor } from './table-block-model';
import { editorExperience, renderExtensions } from '../render-bundle';
import { tableDocSource } from './table-doc-source';

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

afterEach(() => {
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

const press = (el: Element) => el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
const firstCell = (parent: HTMLElement) => parent.querySelector('td') as HTMLElement;
const cellEditors = (parent: HTMLElement) => parent.querySelectorAll('.cm-table-block-cell-editor .cm-editor');

describe('TableBlockWidget — a host that is not editable', () => {
  // TEST-WHY: refused-principal — a commentator's host is editable-false but not readOnly, the principal that must be refused a cell editor and every toolbar write.
  it('a commentator is denied the cell editor and the toolbar', () => {
    const { parent } = mountTable({ commentator: true });
    press(firstCell(parent));
    expect(cellEditors(parent)).toHaveLength(0);
    const rows = parent.querySelectorAll('tr').length;
    parent.querySelectorAll('.cm-table-block-toolbar button').forEach(press);
    expect(parent.querySelectorAll('tr')).toHaveLength(rows);
  });

  // TEST-WHY: refused-principal — access dropping while a cell is open must leave no live editor, since the eq() widget is never rebuilt.
  it('a mounted cell editor is denied once editability is lost', async () => {
    document.elementFromPoint = () => null; // jsdom has no layout; the mount path asks.
    const { view, parent } = mountTable();
    press(firstCell(parent));
    expect(cellEditors(parent)).toHaveLength(1);
    view.dispatch({ effects: editableSlot.reconfigure(EV.editable.of(false)) });
    await Promise.resolve();
    expect(cellEditors(parent)).toHaveLength(0);
  });
});
