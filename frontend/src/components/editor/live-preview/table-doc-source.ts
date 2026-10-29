/**
 * CM6 Facet selecting the Y.Doc a table-block widget reads its model from.
 *
 * Default (no provider / null) → the widget falls back to the LIVE handle
 * (`getActiveHandle().ydoc`), the normal editing case. A snapshot-preview instance
 * provides a preview-local Y.Doc seeded from the checkpoint's `tables_json` so the
 * preview renders THAT captured table state (read-only), never the live editor's
 * current/edited state.
 *
 * Why a Facet and not a constructor arg: the widget is built by the generic
 * block-replace decoration field (`fields.ts`), which has no checkpoint context — it
 * only knows the parsed anchor. The facet lets the host Editor state (which DOES know
 * whether it is a preview) select the doc without threading checkpoint data through the
 * decoration pipeline. Last provider wins (a single preview editor is the only multi-doc
 * case).
 */
// SYSTEM: table-block — doc-source selector facet

import { Facet } from '@codemirror/state';
import type * as Y from 'yjs';

export const tableDocSource = Facet.define<Y.Doc | null, Y.Doc | null>({
  combine: (docs) => docs[docs.length - 1] ?? null,
});

/**
 * Entity id the view renders — the ONE answer to "which entity is this view", read by
 * getViewEntity (table widget late-bind, image drop, paste/drop model placement). The
 * focused slot cannot answer it in split view (it points at whichever column holds
 * focus). A facet because it is set at view construction: the widget's `toDOM` runs
 * during construction, so a host that registered after it would be read too late.
 * Provided by every host (the document editor, TableFocusView, table cells); a view
 * without a provider (PublicEditor, nested transclusion views) reads null.
 */
export const viewEntity = Facet.define<string | null, string | null>({
  combine: (ids) => ids[0] ?? null,
});
