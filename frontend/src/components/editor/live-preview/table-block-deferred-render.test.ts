/**
 * Table block lazy placeholders: the first EAGER_MARKDOWN_RENDERS markdown cells are
 * formatted when the table is built, the rest show raw text and are formatted in deferred
 * time slices — skipped for a cell whose editor mounted first, cancelled with the widget.
 * Harness: a real document EditorView over a table anchor, the table model supplied through
 * the tableDocSource facet (editable state ⇒ lazy placeholders, no nested editors).
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import * as Y from 'yjs';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { editorExperience, renderExtensions } from '../render-bundle';
import { tableDocSource } from './table-doc-source';
import { createTable, tableAnchor } from './table-block-model';
import { EAGER_MARKDOWN_RENDERS } from './table-block-widget';

vi.mock('mermaid', () => ({
  default: { initialize: vi.fn(), render: vi.fn(async () => ({ svg: '<svg></svg>' })) },
}));

const LINK = '[a](https://e.com)';
let view: EditorView | null = null;

function mount(matrix: string[][]): HTMLElement {
  const doc = new Y.Doc();
  const id = createTable(doc, matrix);
  const parent = document.createElement('div');
  document.body.appendChild(parent);
  view = new EditorView({
    parent,
    state: EditorState.create({
      doc: tableAnchor('T', id),
      extensions: [editorExperience({ mode: 'document' }), renderExtensions(), tableDocSource.of(doc)],
    }),
  });
  return parent;
}

function bodyCells(parent: HTMLElement): HTMLElement[] {
  return [...parent.querySelectorAll<HTMLElement>('td .cm-table-block-cell-editor')];
}

afterEach(() => {
  view?.destroy();
  view = null;
  document.body.replaceChildren();
});

describe('table block deferred placeholder render', () => {
  it('formats the eager budget at build and the rest after deferred passes', async () => {
    const parent = mount([['h'], ...Array.from({ length: EAGER_MARKDOWN_RENDERS + 10 }, () => [LINK])]);
    const texts = bodyCells(parent).map((c) => c.textContent);
    expect(texts.slice(0, EAGER_MARKDOWN_RENDERS).every((t) => t === 'a')).toBe(true);
    expect(texts.slice(EAGER_MARKDOWN_RENDERS)).toEqual(Array(10).fill(LINK));

    await vi.waitFor(() => expect(bodyCells(parent).every((c) => c.textContent === 'a')).toBe(true));
  });

  it('plain cells never consume the eager budget', () => {
    const plainRows = Array.from({ length: EAGER_MARKDOWN_RENDERS + 50 }, (_, i) => [`${i}`]);
    const linkRows = Array.from({ length: EAGER_MARKDOWN_RENDERS }, () => [LINK]);
    const parent = mount([['h'], ...plainRows, ...linkRows]);
    const texts = bodyCells(parent).map((c) => c.textContent);
    expect(texts.slice(plainRows.length).every((t) => t === 'a')).toBe(true);
  });

  it('a destroyed table renders nothing more', async () => {
    const parent = mount([['h'], ...Array.from({ length: EAGER_MARKDOWN_RENDERS + 5 }, () => [LINK])]);
    const pending = bodyCells(parent).slice(EAGER_MARKDOWN_RENDERS);
    view!.destroy();
    view = null;
    await new Promise((r) => setTimeout(r, 50));
    expect(pending.map((c) => c.textContent)).toEqual(Array(5).fill(LINK));
  });
});
