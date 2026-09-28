/**
 * Snapshot-preview Editor extensions — the read-only overrides the preview
 * instance layers on top of the LIVE extension array it shares with the editor.
 *
 * // see SYSTEM: editor — preview half of the extension wiring.
 *
 * The preview is a pure VIEWER seeded with a snapshot's DERIVED text. It must
 * have no write path of any kind: `EditorState.readOnly` blocks transaction
 * effects from mutating state; `EditorView.editable=false` disables the
 * contentEditable DOM. table-reconcile is also gated by its enabled predicate
 * and autosave guards itself, but the overrides here are the belt-and-braces
 * layer. Why (a data-loss incident): a preview is seeded where table anchors
 * are gone — any write path that ran there (reconcile, autosave) could destroy
 * live models.
 */
import { EditorState, Prec, type Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import { applyTablesJson } from './live-preview/table-block-model';
import { tableDocSource } from './live-preview/table-doc-source';

/**
 * Build a throwaway Y.Doc seeded with a checkpoint's captured `tables` subtree, for the
 * snapshot-preview Editor to feed the table widgets via the `tableDocSource` facet.
 * `null` (legacy checkpoint, no tables captured) → empty tables map (the widget then
 * shows its readonly "not captured" state). Never the live editor's doc.
 */
export function seedPreviewTablesDoc(tablesJson: string | null): Y.Doc {
  const doc = new Y.Doc();
  applyTablesJson(doc, tablesJson);
  return doc;
}

/**
 * Layer the preview overrides onto the live extension array the preview shares
 * with the editor (identical rendering by construction).
 * Re-built per checkpoint (caller memoizes on the snapshot) so switching
 * snapshots re-seeds the preview tables doc.
 */
export function buildPreviewExtensions(
  cmExtensions: Extension[],
  tablesJson: string | null,
): Extension[] {
  return [
    ...cmExtensions,
    // Highest precedence overrides the shared cmExtensions' editable.of(true) default
    // (a plain editable.of(false) loses facet precedence ties — see table-reconcile.test).
    Prec.highest(EditorState.readOnly.of(true)),
    Prec.highest(EditorView.editable.of(false)),
    // see SYSTEM: table-block — render the CHECKPOINT's captured table state, not the live
    // editor's. A preview-local Y.Doc is seeded from snapshotPreview.tables_json and
    // selected via the tableDocSource facet; the table widget reads this doc (faceted)
    // instead of the live handle, so an older snapshot shows ITS table — and is read-only.
    // tables_json null/absent (legacy) → empty tables map → the widget's readonly
    // missing-model state (a distinct "not captured" placeholder, never the live table).
    tableDocSource.of(seedPreviewTablesDoc(tablesJson)),
  ];
}
