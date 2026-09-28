/**
 * Host-document trigger for table anchor↔model reconciliation.
 *
 * SYSTEM: table-block — the live wiring for `cloneDuplicateAnchors`. The model helper
 * (`table-block-model.ts`) is complete and unit-tested; this extension calls it from
 * the live document editor to handle ONE real-world case:
 *   - copy-paste of a `![label](table:id)` anchor aliased one model to two anchors
 *     (editing either cell mutated the same Y.Text) → clone the duplicate under a
 *     fresh id so each anchor owns a distinct model.
 *
 * INVARIANT(data-loss) (a text edit NEVER deletes a table model): auto orphan-cleanup
 * was REMOVED. Editing/removing/corrupting an anchor in the document text — even
 * mangling a single char of `table:id` — leaves the model untouched. Why: "no matching
 * anchor" was overloaded with two intents ("user removed the anchor" vs "user corrupted
 * it"); the code could not tell them apart, so a single mangled char silently pruned a
 * whole table (~3 s later via the old cleanup timer). Deletion is now EXPLICIT-UI-ONLY
 * (References-badge delete → `deleteTableWithBackup`). A retained orphan model is
 * accepted dead weight (user confirmed snapshot bloat "не болит"); no GC.
 *
 * This extension is a CM6 ViewPlugin (NOT a bare `updateListener`) with ONE debounced
 * timer:
 *  - SHORT (clone): shortly after the text settles, clone any duplicated anchor id
 *    (rewriting the content). Latency-sensitive — a pasted duplicate should not stay
 *    aliased.
 *
 * ARCH: the trigger lives on the host document editor (not the cell editors) because
 * anchors live in the document text and the `tables` subtree is shared by all cells.
 * It reads the live Y.Doc lazily via `getActiveHandle()` — the same pattern the widget
 * uses — so it always targets the focused entity's doc without threading the collab
 * connection through.
 *
 * INVARIANT (fire-time identity guard): the `tables` map and the content the clone
 * pass runs against MUST come from the SAME entity's Y.Doc. The Doc instance is
 * captured at arm time (when a docChanged schedules the timer) and re-verified at fire
 * time — bail unless `getActiveHandle()?.ydoc` is still that exact instance. Why: a
 * clone pass fed by another view's text can rewrite the wrong entity's content
 * (a data-loss incident: a timer fired after a doc switch reading destroyed view A's
 * text while the active handle already pointed at B's ydoc).
 *
 * INVARIANT (no feedback loop). `cloneDuplicateAnchors` rewrites the content ONLY when
 * a duplicate id was cloned (`changed === true`); after the rewrite every id is
 * distinct, so the next pass is a no-op.
 */
import type { Extension } from '@codemirror/state';
import { ViewPlugin, type ViewUpdate, EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import { getActiveHandle } from '../../../editor/active-editor';
import { getTablesMap, cloneDuplicateAnchors } from './table-block-model';

const CLONE_DEBOUNCE_MS = 300;
const ANCHOR_MARKER = '](table:';

/**
 * Debounced reconcile of the `tables` model against the document text. Add to the
 * document editor's extension list (see Editor.tsx `cmExtensions`).
 *
 * `enabled` is a live predicate (same pattern as `liveHeadingsExtension`); the Editor
 * passes `() => !snapshotPreviewRef.current` so the snapshot-preview instance is a
 * pure viewer — belt-and-braces on top of the identity guard (preview edits must not
 * even rewrite the preview text via `cloneDuplicateAnchors`).
 */
export function tableReconcileExtension(enabled: () => boolean = () => true): Extension {
  return ViewPlugin.fromClass(
    class {
      private cloneTimer: ReturnType<typeof setTimeout> | null = null;
      /** Doc captured when the timer was armed — re-verified at fire time. */
      private armedDoc: Y.Doc | null = null;

      update(u: ViewUpdate): void {
        if (!u.docChanged) return;
        if (!enabled()) return;

        if (this.cloneTimer) clearTimeout(this.cloneTimer);
        const view = u.view;

        // Capture the target entity's doc at arm time. Reading the active handle here
        // (not at fire time) binds content+map to the entity the user was editing when
        // the edit happened.
        const armed = getActiveHandle()?.ydoc ?? null;
        this.armedDoc = armed;

        const stillSameDoc = (): boolean => {
          const handle = getActiveHandle();
          return !!handle && !!this.armedDoc && handle.ydoc === this.armedDoc;
        };

        // Fast path: no table model AND no anchor marker → nothing to clone.
        if (armed && getTablesMap(armed).size === 0 &&
            !view.state.doc.toString().includes(ANCHOR_MARKER)) {
          return;
        }

        // SHORT: clone pasted-duplicate anchors (content rewrite) — reacts promptly.
        this.cloneTimer = setTimeout(() => {
          this.cloneTimer = null;
          if (!stillSameDoc() || !armed) return;
          // INVARIANT: the buffer rewrite only runs against the LIVE bound view — i.e.  Why: the rewrite only runs against the live bound view (view text == entity ytext); a stale/derived view on reconnect or a cross-doc timer must not rewrite, else it corrupts the wrong ytext.
          // the view text matches the entity's own ytext. A view seeded with stale /
          // derived (anchor-less) text on reconnect, or a cross-doc timer, must NOT be
          // rewritten. Comparing the view text to the ydoc content is the cheapest
          // proof the view is yCollab-bound to THIS handle's doc.
          const liveContent = armed.getText('content').toString();
          if (view.state.doc.toString() !== liveContent) return;
          const handle = getActiveHandle();
          if (!handle || !handle.synced) return;
          const { content: rewritten, changed } = cloneDuplicateAnchors(armed, liveContent);
          if (!changed) return;
          view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: rewritten } });
        }, CLONE_DEBOUNCE_MS);
      }

      destroy(): void {
        // Structural teardown of the stale-view vector: cancelling the timer on unmount
        // guarantees a destroyed view can never fire a clone pass.
        if (this.cloneTimer) clearTimeout(this.cloneTimer);
        this.cloneTimer = null;
        this.armedDoc = null;
      }
    },
  );
}
