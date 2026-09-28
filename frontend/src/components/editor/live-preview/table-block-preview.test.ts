/**
 * Tests for the snapshot-preview table rendering (Fix 3): a read-only (preview) table
 * widget reads its model from the `tableDocSource` facet (the checkpoint's captured
 * state), NOT the live handle, and renders static cells with no write affordances.
 *
 * The full editable mount is avoided by exercising the readonly path of toDOM, which
 * builds static cells (no nested EditorView). The same readonly toDOM harness also
 * drives the CM6 viewport-eviction regression (destroy → toDOM on the SAME instance).
 */
// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest';
import { EditorView } from '@codemirror/view';
import { EditorState } from '@codemirror/state';
import * as Y from 'yjs';
import { createTable } from './table-block-model';
import { tableDocSource } from './table-doc-source';
import { TableBlockWidget } from './table-block-widget';
import { releaseHandle, publishHandle } from '../../../editor/active-editor';
import type { EntityYjsState } from '../../../collab/yjs-provider';

interface PreviewWidget {
  doc: Y.Doc | null;
  toDOM(view: EditorView): HTMLElement;
  destroy(): void;
  readonly estimatedHeight: number;
}

const views: EditorView[] = [];

function makeView(facetDoc: Y.Doc | null): EditorView {
  const exts = [EditorState.readOnly.of(true)];
  if (facetDoc) exts.push(tableDocSource.of(facetDoc));
  const v = new EditorView({
    state: EditorState.create({ extensions: exts }),
    parent: document.createElement('div'),
  });
  views.push(v);
  return v;
}

afterEach(() => {
  releaseHandle();
  while (views.length) views.pop()!.destroy();
});

describe('tableDocSource facet', () => {
  it('provides the doc when supplied, null when absent', () => {
    const doc = new Y.Doc();
    expect(EditorState.create({ extensions: [tableDocSource.of(doc)] }).facet(tableDocSource)).toBe(doc);
    expect(EditorState.create({ extensions: [] }).facet(tableDocSource)).toBeNull();
  });
});

describe('TableBlockWidget — snapshot preview (readonly)', () => {
  it('prefers the facet doc over the live handle', () => {
    const previewDoc = new Y.Doc();
    const id = createTable(previewDoc, [['h1', 'h2'], ['a', 'b']]);
    // A DIFFERENT live handle — must be ignored in favor of the facet doc.
    const liveDoc = new Y.Doc();
    publishHandle({ ydoc: liveDoc } as unknown as EntityYjsState);

    const widget = new TableBlockWidget(id, 't') as unknown as PreviewWidget;
    const wrap = widget.toDOM(makeView(previewDoc));

    expect(widget.doc).toBe(previewDoc); // facet wins, not the live handle
    // Cells rendered statically from the checkpoint's captured text.
    const cells = wrap.querySelectorAll('.cm-table-block-cell-static');
    expect(cells.length).toBe(4);
    expect([...cells].map((c) => c.textContent)).toEqual(['h1', 'h2', 'a', 'b']);
    widget.destroy();
  });

  it('renders static cells with no editor / toolbar / resize handle', () => {
    const previewDoc = new Y.Doc();
    const id = createTable(previewDoc, [['h'], ['r1'], ['r2']]);
    const widget = new TableBlockWidget(id, 't') as unknown as PreviewWidget;
    const wrap = widget.toDOM(makeView(previewDoc));

    // No write affordances in a pure viewer.
    expect(wrap.querySelector('.cm-table-block-toolbar')).toBeNull();
    expect(wrap.querySelector('.cm-table-block-col-resize')).toBeNull();
    // No nested CM6 editor instances (the .cm-editor root a live cell would mount).
    expect(wrap.querySelector('.cm-editor')).toBeNull();
    expect(wrap.querySelector('.cm-content')).toBeNull();
    // Static cells present (3 rows × 1 col).
    expect(wrap.querySelectorAll('.cm-table-block-cell-static').length).toBe(3);
    widget.destroy();
  });

  it('shows the "not captured" placeholder for a legacy checkpoint (no model)', () => {
    // The facet doc has NO table for the anchor's id → readonly missing-model state.
    const emptyDoc = new Y.Doc();
    const widget = new TableBlockWidget('absent-id', 't') as unknown as PreviewWidget;
    const wrap = widget.toDOM(makeView(emptyDoc));

    const box = wrap.querySelector('.cm-table-block-error');
    expect(box).not.toBeNull();
    // Distinct from the editable missing state: no "remove" affordance in a preview.
    expect(wrap.querySelector('.cm-table-block-remove')).toBeNull();
    widget.destroy();
  });
});

describe('TableBlockWidget — CM6 viewport eviction (destroy → toDOM on the same instance)', () => {
  it('re-renders the table on scroll-back after destroy() reset (regression: blank anchor line)', () => {
    const previewDoc = new Y.Doc();
    const id = createTable(previewDoc, [['h1', 'h2'], ['a', 'b']]);
    const widget = new TableBlockWidget(id, 't') as unknown as PreviewWidget;

    // First mount (widget scrolled into view).
    const wrap1 = widget.toDOM(makeView(previewDoc));
    expect(wrap1.querySelector('table.cm-table-block')).not.toBeNull();
    expect(wrap1.querySelectorAll('.cm-table-block-cell-static').length).toBe(4);

    // CM6 evicts the block widget's DOM from the viewport → destroy(). Scrolling
    // back reuses the SAME instance (eq matches) and calls toDOM again with a
    // FRESH wrap — the render cache must have been reset, or render() takes the
    // same-shape fast path on the stale tableEl and leaves the new wrap EMPTY
    // (the "tables vanish after eviction" bug).
    widget.destroy();
    const wrap2 = widget.toDOM(makeView(previewDoc));
    expect(wrap2.querySelector('table.cm-table-block')).not.toBeNull();
    expect(wrap2.querySelectorAll('.cm-table-block-cell-static').length).toBe(4);
    widget.destroy();
  });

  it('keeps the height estimate across eviction (destroy must not zero the row count)', () => {
    const previewDoc = new Y.Doc();
    const id = createTable(previewDoc, [['h1', 'h2'], ['a', 'b'], ['c', 'd']]);
    const widget = new TableBlockWidget(id, 't') as unknown as PreviewWidget;

    widget.toDOM(makeView(previewDoc));
    const mounted = widget.estimatedHeight;
    // CM6 asks estimatedHeight precisely for widgets whose DOM it dropped, so the
    // evicted estimate must still describe a 3-row table — otherwise an off-screen
    // table collapses to padding height and the scrollbar jumps on scroll-back.
    widget.destroy();
    expect(widget.estimatedHeight).toBe(mounted);
  });
});
