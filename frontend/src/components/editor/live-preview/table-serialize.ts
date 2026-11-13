/** Table serialization — checkpoint JSON, GFM and clipboard-HTML projections of the model (twin of `backend/table_serialize.py`; see SYSTEM: table-block, table-block-model.ts). */
import type * as Y from 'yjs';
import type { EditorView } from '@codemirror/view';
import {
  TABLE_SYSTEM_ORIGIN, buildTable, getTablesMap, readTableModel, resolveMerges, tableFormatUnknown,
  type TableModel,
} from './table-block-model';
import { DEFAULT_COLUMN_WIDTH, freezeAutoWidths } from './table-widths';
import { TABLE_ANCHOR_RE } from './table-document-ops';
import { islandToken, renderCellHtml, type Island } from '../markdown-to-html';

/**
 * Serialize the whole `tables` subtree to compact JSON for checkpoint capture.
 *
 * INVARIANT (shared shape, frontend ↔ backend): the output MUST be byte-identical in
 * SHAPE to the backend `capture_tables_json`:
 * `{ "<id>": { columns: number[], rows: string[][], merges?: number[][] } }`, key order
 * `columns, rows, merges` — POSITIONAL, display order, ids dropped; `merges` holds the
 * accepted areas `[r0, c0, r1, c1]` in row-major anchor order, emitted ONLY when non-empty
 * (absent = no merges, so old checkpoints restore unchanged). Why: a checkpoint is restored by whichever
 * replica/producer captured it; silent shape drift (e.g. dropping column widths)
 * rebuilds a default-width table. `table-serialize.test.ts` shares one fixture with
 * the backend `test_table_capture.py` to guard this.
 *
 * A table in an unreadable format is OMITTED, never emitted empty (see
 * `tableFormatUnknown`). `'{}'` when there are no tables. Capture is lossless (cell
 * text verbatim, no GFM escaping).
 */
export function serializeTables(doc: Y.Doc): string {
  const tables = getTablesMap(doc);
  const out: Record<string, { columns: number[]; rows: string[][]; merges?: number[][] }> = {};
  tables.forEach((table, id) => {
    if (tableFormatUnknown(table)) return;
    const model = readTableModel(doc, id);
    if (!model) return;
    const merges = resolveMerges(doc, id).map((a) => [a.r0, a.c0, a.r1, a.c1]);
    out[id] = merges.length ? { columns: model.columns, rows: model.rows, merges } : { columns: model.columns, rows: model.rows };
  });
  return JSON.stringify(out);
}

/**
 * Rebuild the `tables` subtree from `serializeTables` output (checkpoint restore).
 *
 * `null` = legacy restore (no tables captured): leave the tables map untouched.
 * `'{}'` / empty = clear to no tables. Otherwise clear + rebuild every table as v2 with
 * FRESH row/column ids in ONE `doc.transact()` so the rebuild is a single atomic Yjs
 * update — the broadcast frame then carries the whole table rebuild and other replicas
 * converge without an intermediate orphan-anchor state. Mirrors the backend
 * `apply_tables_json`.
 *
 * An unreadable-format table the snapshot does not name is KEPT (capture skipped it — see
 * `tableFormatUnknown` — so the snapshot holds no truth about it). Runs under
 * `TABLE_SYSTEM_ORIGIN`: seeding is outside the undo stack (see the origins ARCH).
 */
export function applyTablesJson(doc: Y.Doc, tablesJson: string | null): void {
  if (tablesJson === null) return;
  const data: Record<string, { columns?: number[]; rows?: string[][]; merges?: unknown[] }> = JSON.parse(tablesJson || '{}');
  const tables = getTablesMap(doc);

  doc.transact(() => {
    tables.forEach((table, id) => {
      if (data[id] !== undefined || !tableFormatUnknown(table)) tables.delete(id);
    });
    Object.entries(data).forEach(([id, model]) => {
      const columns = model.columns ?? [];
      const rows = model.rows ?? [];
      let width = columns.length;
      if (width === 0 && rows.length) width = Math.max(...rows.map((r) => r.length));
      // WHY: an old checkpoint may hold legacy 0 widths; a restore never brings one back.
      const widths = freezeAutoWidths(columns.length ? columns : Array.from({ length: width }, () => DEFAULT_COLUMN_WIDTH), rows);
      buildTable(tables, id, widths, rows, model.merges ?? []);
    });
  }, TABLE_SYSTEM_ORIGIN);
}

/**
 * Serialize a table model to GFM pipe-table markdown (the byte-inverse of
 * `gfm-table-import.ts`'s `parseRow`/`unescapeCell`).
 *
 * model→GFM export (clipboard copy path; see SYSTEM: table-block). The editable table lives as
 * a Yjs subtree; this is the read-only serializer that turns it back into portable pipe
 * markdown so a copied table becomes real `| a | b |` text in `text/plain` and a `<table>`
 * in `text/html` (via `cleanMarkdownToHtml`). Never throws.
 *
 * INVARIANT: cell escaping is the exact inverse of the importer — `\n`→`<br>`, `|`→`\|`,
 * exactly one padding space per cell side, `rows[0]` = header, separator = `| --- | … |`.
 * Why: a copy must round-trip back into a native table object on paste (see
 * `gfm-table-import.ts:unescapeCell`/`parseRow`). Read-only on the Y.Doc.
 *
 * Known limitation (inherent to GFM, not a bug): a header ROW whose EVERY cell body matches
 * the separator grammar `/^:?-+:?$/` (e.g. `| --- | :--: |`) cannot be represented — GFM has
 * no way to mark such a row as data rather than separator, so the importer misclassifies it
 * (gfm-table-import.ts:isSeparatorRow) and the table is not recognized. A single separator-
 * grammar cell alongside a normal cell is fine (the row only reads as a separator when all
 * its cells are dash-like). Rare in practice; body cells are unaffected.
 */
export function modelToGfm(model: TableModel): string {
  const rows = model.rows;
  if (rows.length === 0) return '';
  const width = Math.max(model.columns.length, ...rows.map((r) => r.length), 1);
  // Inverse of unescapeCell: pipe first (so a cell's own `|` is escaped before padding),
  // then newline→<br>. Order is load-bearing for invertibility across mixed cell content.
  const esc = (cell: string) => cell.replace(/\|/g, '\\|').replace(/\n/g, '<br>');
  const rowToLine = (cells: string[]) => {
    const padded = cells.slice(0, width);
    while (padded.length < width) padded.push('');
    return `|${padded.map((c) => ` ${esc(c)} `).join('|')}|`;
  };
  const header = rowToLine(rows[0]);
  const sep = `|${Array.from({ length: width }, () => ' --- ').join('|')}|`;
  const body = rows.slice(1).map(rowToLine);
  return [header, sep, ...body].join('\n');
}

/**
 * Replace every `![label](table:id)` anchor in `markdown` with the GFM serialization of its
 * model. A model that is missing (orphan anchor / mount race / unreadable format) is left
 * untouched — the copy handler degrades gracefully and never throws. Read-only on the Y.Doc.
 */
export function expandTableAnchorsToGfm(doc: Y.Doc, markdown: string): string {
  return markdown.replace(TABLE_ANCHOR_RE, (full, _label: string, id: string) => {
    const model = readTableModel(doc, id);
    if (!model) return full;
    return modelToGfm(model);
  });
}

/**
 * Render a table model as clipboard HTML for external editors (Word / Docs / Sheets): row 0
 * in `<thead>`, the rest in `<tbody>`; an area's anchor cell carries `colspan`/`rowspan`
 * (omitted when 1) and its covered cells are not emitted. Built from the model, not from GFM:
 * GFM has no spans and its pipe grammar would split a cell holding `|`.
 */
export function modelToHtml(model: TableModel, areas: readonly { r0: number; c0: number; r1: number; c1: number }[]): string {
  const rows = model.rows;
  if (rows.length === 0) return '';
  const width = Math.max(model.columns.length, ...rows.map((r) => r.length), 1);
  const anchors = new Map(areas.map((a) => [`${a.r0}:${a.c0}`, a]));
  const covered = new Set(areas.flatMap((a) => {
    const keys: string[] = [];
    for (let r = a.r0; r <= a.r1; r++) for (let c = a.c0; c <= a.c1; c++) if (r !== a.r0 || c !== a.c0) keys.push(`${r}:${c}`);
    return keys;
  }));
  const rowHtml = (cells: string[], r: number, tag: 'th' | 'td') => {
    let out = '';
    for (let c = 0; c < width; c++) {
      if (covered.has(`${r}:${c}`)) continue;
      const a = anchors.get(`${r}:${c}`);
      const colspan = a && a.c1 > a.c0 ? ` colspan="${a.c1 - a.c0 + 1}"` : '';
      const rowspan = a && a.r1 > a.r0 ? ` rowspan="${a.r1 - a.r0 + 1}"` : '';
      out += `<${tag}${colspan}${rowspan}>${renderCellHtml(cells[c] ?? '')}</${tag}>`;
    }
    return `<tr>${out}</tr>`;
  };
  const body = rows.slice(1).map((cells, i) => rowHtml(cells, i + 1, 'td')).join('');
  return `<table><thead>${rowHtml(rows[0], 0, 'th')}</thead><tbody>${body}</tbody></table>`;
}

/**
 * Replace every table anchor in `markdown` with an `islandToken` and return the rendered
 * tables (`modelToHtml`, with `modelToGfm` as the text form) in token order — the `islands` of `cleanMarkdownToHtml`. A missing
 * model leaves its anchor untouched (copy never throws). Read-only on the Y.Doc.
 */
export function tableAnchorsToIslands(doc: Y.Doc, markdown: string): { text: string; islands: Island[] } {
  const islands: Island[] = [];
  const text = markdown.replace(TABLE_ANCHOR_RE, (full, _label: string, id: string) => {
    const model = readTableModel(doc, id);
    if (!model) return full;
    islands.push({ html: modelToHtml(model, resolveMerges(doc, id)), text: modelToGfm(model) });
    return islandToken(islands.length - 1);
  });
  return { text, islands };
}

/**
 * The merged areas of every table anchor in `markdown`, keyed by that table's GFM
 * projection (`modelToGfm`) — the Lore→Lore clipboard sidecar: GFM has no spans, so the
 * paste side re-applies them to the table it builds from the identical GFM block.
 */
export function tableMergesByGfm(doc: Y.Doc, markdown: string): [string, number[][]][] {
  const out: [string, number[][]][] = [];
  for (const [, , id] of markdown.matchAll(TABLE_ANCHOR_RE)) {
    const model = readTableModel(doc, id);
    const merges = model ? resolveMerges(doc, id).map((a) => [a.r0, a.c0, a.r1, a.c1]) : [];
    if (model && merges.length) out.push([modelToGfm(model), merges]);
  }
  return out;
}

/**
 * The ONE client projection of "the document text for parsing": the view's host text with
 * every table anchor expanded to its cells. WHY: cell text lives in the table model, not the
 * host text — scanning the host alone missed links/refs that live only in a cell. The ydoc is
 * a parameter because `active-editor` imports this module (resolving it here would cycle).
 */
export function readModelText(view: EditorView, ydoc: Y.Doc | undefined): string {
  const text = view.state.doc.toString();
  return ydoc ? expandTableAnchorsToGfm(ydoc, text) : text;
}
