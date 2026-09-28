// @vitest-environment jsdom
/**
 * Tests for preview-editor-extensions — the snapshot-preview Editor's overrides:
 * seedPreviewTablesDoc (checkpoint tables -> a throwaway Y.Doc) and
 * buildPreviewExtensions (read-only pair + tableDocSource facet).
 *
 * see SYSTEM: table-block — the preview renders the CHECKPOINT's captured table
 * state, never the live editor's doc.
 */

import { describe, it, expect } from 'vitest';
import { EditorView } from '@codemirror/view';
import { EditorState, type Extension } from '@codemirror/state';
import * as Y from 'yjs';
import {
  seedPreviewTablesDoc,
  buildPreviewExtensions,
} from './preview-editor-extensions';
import {
  createTable,
  serializeTables,
  readTableModel,
  getTablesMap,
} from './live-preview/table-block-model';
import { tableDocSource } from './live-preview/table-doc-source';

describe('seedPreviewTablesDoc', () => {
  it('null (legacy checkpoint, nothing captured) seeds an empty tables map', () => {
    const doc = seedPreviewTablesDoc(null);
    expect(getTablesMap(doc).size).toBe(0);
  });

  it('round-trips a captured table through the serialize/apply pair', () => {
    const source = new Y.Doc();
    createTable(source, [['h1', 'h2'], ['a', 'b']]);
    const json = serializeTables(source);
    const preview = seedPreviewTablesDoc(json);
    const model = [...getTablesMap(preview).keys()][0];
    expect(readTableModel(preview, model)).toEqual({
      columns: expect.any(Array),
      rows: [['h1', 'h2'], ['a', 'b']],
    });
  });
});

describe('buildPreviewExtensions', () => {
  it('overrides to read-only, non-editable, with the seeded table doc faceted', () => {
    const source = new Y.Doc();
    const id = createTable(source, [['x']]);
    const json = serializeTables(source);
    const base: Extension[] = [EditorView.editable.of(true)];
    const view = new EditorView({
      state: EditorState.create({
        doc: 'preview body',
        extensions: [...base, ...buildPreviewExtensions(base, json)],
      }),
      parent: document.createElement('div'),
    });
    expect(view.state.facet(EditorState.readOnly)).toBe(true);
    expect(view.state.facet(EditorView.editable)).toBe(false);
    const faceted = view.state.facet(tableDocSource);
    expect(faceted).toBeInstanceOf(Y.Doc);
    expect(getTablesMap(faceted as Y.Doc).has(id)).toBe(true);
    view.destroy();
  });
});
