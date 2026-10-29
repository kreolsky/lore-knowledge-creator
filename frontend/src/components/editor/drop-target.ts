/** The editor drop surface shared by the file drop (useEditorFileDrop) and the
 * reference-card drag (refDragAdapter): target view + position, splice, stored size. */

import { EditorView } from '@codemirror/view';
import type { Text } from '@codemirror/state';
import { mountLazyCell } from './live-preview/table-block-widget';
import { contentBoxWidth, imageEmbedParts, setImageSize } from './live-preview/widgets';

/** The cell editor under a td/th hit: the mounted view, a lazy cell mounted now (gated
 * by the OUTER view's editability), null (read-only), or undefined for a static preview
 * cell — the caller falls through to the outer view, whose gate rejects it. */
function cellDropView(td: Element): EditorView | null | undefined {
  const host = td.querySelector(':scope > .cm-table-block-cell-editor');
  if (!host) return undefined;
  const cellCm = host.querySelector('.cm-editor');
  if (cellCm) {
    const view = EditorView.findFromDOM(cellCm as HTMLElement);
    return view && view.state.facet(EditorView.editable) ? view : null;
  }
  if (!host.classList.contains('cm-table-block-cell-lazy')) return undefined;
  const outerCm = td.closest('.cm-editor');
  const outer = outerCm ? EditorView.findFromDOM(outerCm as HTMLElement) : null;
  if (!outer || !outer.state.facet(EditorView.editable)) return null;
  return mountLazyCell(host);
}

/** The editable editor surface under a pointer position: the innermost
 * `.cm-editor` under the coordinates whose view passes the editable gate
 * (`EditorView.editable` — the same value that drives the file-drop guard:
 * readonly, disconnected, commentator and public-share views all fold into
 * it). Read-only nested views and plain DOM are not drop surfaces.
 *
 * WHY a cell is hit by its td/th: its editor is content-sized, so bare cell space
 * holds no `.cm-editor` of the cell, while a CLICK there routes into it (the
 * widget's mousedown router). A lazy cell is mounted here, so the drop caret shows
 * inside it as in the document; the position is the nearest one, like a click. */
export function editorDropPoint(x: number, y: number): { view: EditorView; pos: number } | null {
  const hit = document.elementFromPoint(x, y);
  if (!(hit instanceof Element)) return null;
  const td = hit.closest('.cm-table-block td[data-row][data-col], .cm-table-block th[data-row][data-col]');
  const cell = td ? cellDropView(td) : undefined;
  if (cell !== undefined) return cell ? { view: cell, pos: cell.posAtCoords({ x, y }, false) } : null;
  const cm = hit.closest('.cm-editor');
  if (!cm) return null;
  const view = EditorView.findFromDOM(cm as HTMLElement);
  if (!view || !view.state.facet(EditorView.editable)) return null;
  const pos = view.posAtCoords({ x, y });
  return pos === null || pos === undefined ? null : { view, pos };
}

/**
 * Where dropped content lands — inline vs block.
 * INLINE content (an image embed `![title](ref:id)`) is inserted at `pos`
 * verbatim. BLOCK content (a table anchor, imported markdown) must occupy its
 * own line: a `\n` is added before it unless `pos` is already at a line start,
 * and after it unless `pos` is already at a line end. `pos` is clamped to the
 * doc — an async upload may return after remote edits shrank it.
 */
export function dropInsertSpec(
  doc: Text,
  pos: number,
  text: string,
  kind: 'inline' | 'block',
): { from: number; insert: string } {
  const from = Math.min(Math.max(pos, 0), doc.length);
  if (kind === 'inline') return { from, insert: text };
  const line = doc.lineAt(from);
  return {
    from,
    insert: `${from === line.from ? '' : '\n'}${text}${from === line.to ? '' : '\n'}`,
  };
}

/** The tail every drop shares: splice a prepared `dropInsertSpec` into `view`,
 * land the caret after the inserted text, focus the view. */
export function spliceAt(view: EditorView, spec: { from: number; insert: string }): void {
  view.dispatch({
    changes: { from: spec.from, insert: spec.insert },
    selection: { anchor: spec.from + spec.insert.length },
  });
  view.focus();
}

/** An embed written by a DROP into `view`: an image landing in a table CELL is stored
 * at its drawn width, `|min(naturalWidth, cell content width)` px (unknown natural ⇒
 * the cell width). WHY cell-only: a column width is document data, the document
 * column depends on the viewer's window — so everything else stays verbatim. */
export function dropEmbedForView(view: EditorView, embed: string, image: boolean, naturalWidth?: number): string {
  if (!image || !view.dom.closest('.cm-table-block')) return embed;
  const cellWidth = contentBoxWidth(view.contentDOM);
  const parts = imageEmbedParts(embed);
  if (!parts || cellWidth <= 0) return embed;
  const width = Math.floor(naturalWidth ? Math.min(naturalWidth, cellWidth) : cellWidth);
  return `![${setImageSize(parts.alt, String(width))}](${parts.url})`;
}
