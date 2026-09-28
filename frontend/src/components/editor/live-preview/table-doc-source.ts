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
 * Entity id of the host view's document — the identity the table widget late-binds
 * its live ydoc through (getViewEntity → subscribeEntityHandle). A facet (not the
 * WeakMap alone) because the widget's `toDOM` runs during view construction, BEFORE
 * the host's `onCreateView` registers the view→entity pair: a TableFocusView whose
 * doc text IS the anchor mounts its widget at construction time. Hosts without the
 * facet fall back to the module registry (`getViewEntity(view)`).
 */
export const tableHostEntity = Facet.define<string, string | null>({
  combine: (ids) => ids[0] ?? null,
});
