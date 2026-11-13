/** Table document ops — anchor/label edits on the `content` text and whole-table create/delete paths (see SYSTEM: table-block, table-block-model.ts). */
import * as Y from 'yjs';
import { uuid } from '../../../utils/uuid';
import { apiClient } from '../../../api/client';
import {
  TABLE_LOCAL_ORIGIN, TABLE_SYSTEM_ORIGIN, buildTable, cloneTable, getModelLabel, getTablesMap,
  localStep, readTableShape, setModelLabel,
} from './table-block-model';
import { birthColumnWidths } from './table-widths';

/**
 * Create a table model from a cell matrix AND store its label — with NO anchor spliced into
 * the document `content`. Returns the fresh table id.
 *
 * WHY: the CSV/TSV panel-import path (see `csv-table.ts`) absorbs a file fully into a table
 * model; the table then surfaces as a panel-resident "virtual reference" badge (reactively
 * via `useDocumentTables`) without occupying a line in the body. The label is the model-level
 * fallback (see `setModelLabel`) — it seeds the anchor if the user later inserts the table.
 *
 * INVARIANT(atomic-write): model build + label set run in ONE `doc.transact`.
 * Why: two separate transactions could be observed mid-way by a peer or by the reactive
 * `useDocumentTables` derive — a badge with an empty label that then snaps to the real name.
 * One atomic update makes an import a single consistent table-with-label Yjs state.
 */
export function createUnlinkedTable(doc: Y.Doc, matrix: string[][], label: string): string {
  let id = '';
  doc.transact(() => {
    id = uuid();
    buildTable(getTablesMap(doc), id, birthColumnWidths(matrix), matrix);
    const table = getTablesMap(doc).get(id)!;
    table.set('label', normalizeTableLabel(label));
  }, TABLE_LOCAL_ORIGIN);
  return id;
}

/** Build a table transclusion anchor for the document text. */
export function tableAnchor(label: string, id: string): string {
  return `![${label}](table:${id})`;
}

/** Matches every `![label](table:id)` anchor; group 1 = label, group 2 = id. */
export const TABLE_ANCHOR_RE = /!\[([^\]]*)\]\(table:([^)]+)\)/g;

/**
  * Clone a DUPLICATED anchor id (copy-paste) under a fresh id and rewrite the content so
  * each anchor owns a distinct model (no aliasing). The live trigger is the debounced
  * `tableReconcileExtension` (see `table-reconcile.ts`); orphan MODELS are never pruned
  * here — see the INVARIANT(data-loss) in the header.
  *
  * Returns the (possibly rewritten) content + a `changed` flag. The caller is responsible
  * for writing `content` back into the text buffer when `changed` (the model mutations are
  * already applied to the Y.Doc here).
  *
  * Runs under `TABLE_SYSTEM_ORIGIN` (outside the undo stack): a background repair, not a
  * user action — see the origins ARCH in the header.
  *
  * INVARIANT: an orphan ANCHOR (anchor present, no model) is NOT created here — it renders
  * an inline error in the widget. Why: content is the user's authored text; we never
  * silently delete an anchor they typed.
  */
export function cloneDuplicateAnchors(doc: Y.Doc, content: string): { content: string; changed: boolean } {
  const tables = getTablesMap(doc);
  const seen = new Set<string>();
  const referenced = new Set<string>();
  let changed = false;
  let rewritten = content;

  doc.transact(() => {
    rewritten = content.replace(TABLE_ANCHOR_RE, (full, label: string, id: string) => {
      if (!seen.has(id)) {
        seen.add(id);
        referenced.add(id);
        return full;
      }
      // Duplicate id → clone the source model under a fresh id, rewrite this anchor.
      if (!tables.has(id)) {
        // Orphan duplicate (no model to clone) — leave as-is; widget shows the error.
        return full;
      }
      const newId = cloneTable(doc, id); // nested transact: inherits the system origin
      changed = true;
      seen.add(newId);
      referenced.add(newId);
      return tableAnchor(label, newId);
    });
  }, TABLE_SYSTEM_ORIGIN);

  return { content: rewritten, changed };
}

/**
 * Delete a WHOLE table (anchor + model) — the one path for the References badge, the cell
 * menu (both via `deleteTableWithCheckpoint`) and the widget's orphan-anchor remove button.
 *
 * In a single `doc.transact`: locate `tableAnchor(label, id)` in `doc.getText('content')`
 * and delete that range, then `deleteTable(doc, id)`. A missing anchor is tolerated (the
 * model is still dropped — orphan cleanup); a missing model is a no-op. Precedent for
 * editing the content Y.Text directly (not via a CM6 view): table-reconcile.ts.
 *
 * INVARIANT: same ydoc as the inline editor's yCollab-bound `content`. One widget action =
 * one atomic Yjs update (mirrors the model-helper write-path INVARIANT above).
 * Why: on the same ydoc Yjs merges the delete correctly and the editor re-renders the
 * now-empty anchor range.
 */
export function deleteTableFromDoc(doc: Y.Doc, tableId: string, label: string): void {
  const content = doc.getText('content');
  const text = content.toString();
  const needle = tableAnchor(label, tableId);
  const at = text.indexOf(needle);
  localStep(doc, () => {
    if (at >= 0) content.delete(at, needle.length);
    getTablesMap(doc).delete(tableId);
  });
}

/**
 * Delete a whole table WITH a backup recovery point (References-badge delete path).
 *
 * Awaits `backup()` (a forced checkpoint POST — captures the LIVE server ydoc while the
 * table is still present) BEFORE `deleteTableFromDoc` mutates the map. `backup` is injected
 * (the model layer stays free of the api/i18n/store coupling the caller owns).
 *
 * INVARIANT(data-loss): the backup is awaited and must RESOLVE before any mutation; if it
 * REJECTS the delete is aborted (the rejection propagates, the model is left intact). Why:
 * a table delete with no recovery point is unrecoverable user-data loss, and a "backup"
 * that ran after (or in parallel with) the delete could capture the already-deleted state
 * (No silent degradation — the caller surfaces the error).
 */
export async function deleteTableWithBackup(
  doc: Y.Doc,
  tableId: string,
  label: string,
  backup: () => Promise<void>,
): Promise<void> {
  await backup();
  deleteTableFromDoc(doc, tableId, label);
}

/**
 * The one whole-table delete of the UI (References badge, cell context menu): a forced
 * checkpoint labelled `backupLabel` (content: null → the server captures the LIVE ydoc,
 * table still present), then the delete. Throws on failure, nothing deleted — see the
 * `deleteTableWithBackup` INVARIANT(data-loss); the caller surfaces the error.
 */
export async function deleteTableWithCheckpoint(
  ydoc: Y.Doc,
  documentId: string,
  tableId: string,
  label: string,
  backupLabel: string,
): Promise<void> {
  await deleteTableWithBackup(ydoc, tableId, label, async () => {
    await apiClient.post('/checkpoints', { document_id: documentId, content: null, label: backupLabel });
  });
}

/**
 * Sanitize a user-typed table label so it can never break the `![label](table:id)`
 * anchor grammar. `]` ends the alt-text (`[^\]]*` in `TABLE_ANCHOR_RE`) and a newline
 * would split the anchor across two lines; both are neutralized. Applied at the write
 * path (renameTable) so a rename can never produce an unparseable anchor.
 *
 * INVARIANT: the output contains no `]` and no newline. Why: an anchor with either is
 * either unparseable or parses to a truncated label — a silent corruption of `content`.
 * `(`, `[`, spaces, unicode all survive (they do not break the grammar).
 */
export function normalizeTableLabel(raw: string): string {
  return raw.replace(/]/g, '').replace(/\n/g, ' ').trim();
}

/**
 * Rename a table's anchor LABEL (the References-panel "table badge" rename path).
 *
 * In a single `doc.transact`: store the normalized label on the model (fallback, see
 * `setModelLabel`) AND — when the table is LINKED — replace its `![oldLabel](table:id)`
 * anchor range in `content` with the renamed anchor. An UNLINKED table (anchor absent) is
 * still renamed via the model label alone, so an orphan table can be named and keep that
 * name when it is re-inserted. Same direct-content-edit precedent as `deleteTableFromDoc`.
 *
 * INVARIANT: the new label is normalized first, so the resulting anchor always re-parses
 * via `TABLE_ANCHOR_RE`. Why: a typed `]`/newline would otherwise corrupt the anchor
 * (see `normalizeTableLabel`). One widget action = one atomic Yjs update (mirrors the
 * model-helper write-path INVARIANT).
 */
export function renameTable(doc: Y.Doc, tableId: string, oldLabel: string, newLabel: string): void {
  const normalized = normalizeTableLabel(newLabel);
  const content = doc.getText('content');
  const needle = tableAnchor(oldLabel, tableId);
  const at = content.toString().indexOf(needle);
  localStep(doc, () => {
    setModelLabel(doc, tableId, normalized);
    if (at >= 0) {
      content.delete(at, needle.length);
      content.insert(at, tableAnchor(normalized, tableId));
    }
  });
}

/**
 * Re-insert a table's transclusion anchor into `content` so a broken/unlinked table becomes
 * reachable in the document again. Appends `\n\n![label](table:id)\n` at the end of the
 * content when no view offset is given; the label is seeded from the model fallback (see
 * `setModelLabel`). One atomic Yjs update. Callers with an editor view should instead insert
 * at the cursor (see ReferencesPanel) — this is the no-view fallback.
 */
export function insertTableAnchor(doc: Y.Doc, tableId: string, label: string): void {
  const content = doc.getText('content');
  const anchor = tableAnchor(normalizeTableLabel(label), tableId);
  localStep(doc, () => {
    content.insert(content.length, `\n\n${anchor}\n`);
  });
}

/** One row of `listDocumentTables` — a table badge's identity + shape (no cell text). */
export interface DocumentTableEntry {
  table_id: string;
  label: string;
  rows: number;
  cols: number;
  /**
   * True when the model exists in the ydoc but NO anchor in `content` references it
   * (anchor removed or its `table:id` corrupted). The label lives only in the anchor, so
   * an unlinked table has none (`''`) and cannot be renamed — but its data is intact and
   * it stays reachable (open / delete / restore-by-fixing-the-anchor) from the panel.
   */
  unlinked: boolean;
  /** Set (only) when the model exists but is not in a readable format; `rows`/`cols` are 0. */
  unreadable?: true;
}

/** Shape fields of an EXISTING model; `null` for an id with no model (an orphan anchor). */
function entryShape(doc: Y.Doc, id: string): Pick<DocumentTableEntry, 'rows' | 'cols' | 'unreadable'> | null {
  const shape = readTableShape(doc, id);
  if (shape) return shape;
  // WHY: an unreadable model is still listed — see the INVARIANT on `listDocumentTables`.
  return getTablesMap(doc).has(id) ? { rows: 0, cols: 0, unreadable: true } : null;
}

/**
 * Derive the panel list of a document's tables from the LIVE ydoc. Read-only; the pure half
 * of the reactive `useDocumentTables` hook.
 *
 * Two ordered groups:
 *  1. LINKED tables — every `![label](table:id)` anchor in `content` joined with its model's
 *     shape, in document (anchor) order (`unlinked: false`).
 *  2. UNLINKED tables — models present in the ydoc that NO surviving anchor references,
 *     appended AFTER the linked group (`unlinked: true`, empty label).
 *
 * INVARIANT: a model that exists in the ydoc is ALWAYS listed (linked or unlinked) — a table
 * is never hidden from the panel just because its anchor was removed/corrupted. Why: hiding
 * it makes intact data unreachable (the user's only handle to open/delete/restore it is this
 * list); the earlier "orphan model excluded" behavior meant a single mangled char in an
 * anchor visually erased a still-present table. Orphan ANCHORS (anchor, no model) are still
 * excluded (nothing to show); a stray duplicate id yields only its first occurrence.
 */
export function listDocumentTables(doc: Y.Doc): DocumentTableEntry[] {
  const content = doc.getText('content').toString();
  const seen = new Set<string>();
  const out: DocumentTableEntry[] = [];
  for (const m of content.matchAll(TABLE_ANCHOR_RE)) {
    const label = m[1] as string;
    const id = m[2] as string;
    if (seen.has(id)) continue;
    const shape = entryShape(doc, id);
    if (!shape) continue;
    seen.add(id);
    out.push({ table_id: id, label, ...shape, unlinked: false });
  }
  // Unlinked models (no anchor references them) — appended below the linked group.
  for (const id of getTablesMap(doc).keys()) {
    if (seen.has(id)) continue;
    const shape = entryShape(doc, id);
    if (!shape) continue;
    seen.add(id);
    out.push({ table_id: id, label: getModelLabel(doc, id), ...shape, unlinked: true });
  }
  return out;
}
