/**
 * Host-document trigger for table anchor↔model reconciliation.
 *
 * SYSTEM: table-block — the live wiring for `cloneDuplicateAnchors`. The model helper
 * (`table-document-ops.ts`) is complete and unit-tested; this extension calls it from
 * the live document editor to handle TWO real-world cases:
 *   - copy-paste of a `![label](table:id)` anchor aliased one model to two anchors
 *     (editing either cell mutated the same Y.Text) → clone the duplicate under a
 *     fresh id so each anchor owns a distinct model (debounced);
 *   - a local edit that removed an anchor WHOLE drops its model (`dropWholeRemovedModels`).
 *
 * INVARIANT(data-loss): a table removed WHOLE from the document by a local edit is deleted completely.
 * Why: the operator — «таблицу больше нельзя удалить одним неаккуратным движением, теперь
 * это уже вполне себе осознанное действие, поэтому при удалении таблицы из документа
 * удаляем её полностью» (one-key deletes are refused by atomic-delete-guard). The model
 * goes only when no anchor with that id is left in the text; a corrupted anchor (a mangled
 * char of `table:id`) never matches whole, so its model is retained (orphan, listed
 * unlinked in the References tab) — auto-cleanup keyed on "no matching anchor" could not
 * tell removal from corruption and lost whole tables on one mangled char.
 *
 * This extension is a CM6 ViewPlugin (NOT a bare `updateListener`) with ONE debounced
 * timer (the whole-removal drop is not debounced, see `dropWholeRemovedModels`):
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
import type { Extension, Transaction } from '@codemirror/state';
import { ViewPlugin, type ViewUpdate, EditorView } from '@codemirror/view';
import { ySyncAnnotation } from 'y-codemirror.next';
import * as Y from 'yjs';
import { entityYdoc, getActiveHandle } from '../../../editor/active-editor';
import { getTablesMap, deleteTable } from './table-block-model';
import { cloneDuplicateAnchors, TABLE_ANCHOR_RE } from './table-document-ops';

const CLONE_DEBOUNCE_MS = 300;
const ANCHOR_MARKER = '](table:';

/** The part of `deleted` that `inserted` really replaced — their common prefix/suffix cut. */
function changedCore(deleted: string, inserted: string): string {
  const max = Math.min(deleted.length, inserted.length);
  let head = 0;
  while (head < max && deleted[head] === inserted[head]) head++;
  let tail = 0;
  while (tail < max - head && deleted[deleted.length - 1 - tail] === inserted[inserted.length - 1 - tail]) tail++;
  return deleted.slice(head, deleted.length - tail);
}

/**
 * Ids of table anchors a transaction deleted WHOLE — the anchor regex matched inside the
 * changed core of a replaced slice of the start doc. A partial deletion cannot match (`![`
 * and the closing `)` are both required), so a corrupted anchor never qualifies.
 * WHY the core and not the raw slice: a wide replace (a full-text rewrite) that only
 * mangles one char of `table:id` deletes the intact anchor inside its raw slice; cut to
 * what actually changed, the anchor is no longer whole in it.
 */
function wholeRemovedAnchorIds(tr: Transaction): string[] {
  const ids: string[] = [];
  tr.changes.iterChanges((fromA, toA, _fromB, _toB, inserted) => {
    if (fromA === toA) return;
    const core = changedCore(tr.startState.doc.sliceString(fromA, toA), inserted.toString());
    for (const m of core.matchAll(TABLE_ANCHOR_RE)) ids.push(m[2] as string);
  });
  return ids;
}

/**
 * Drop the model of every anchor a LOCAL transaction of this update removed whole — see
 * the module INVARIANT(data-loss).
 * WHY skip `ySyncAnnotation`: remote peers and Y.UndoManager undo/redo arrive that way, and
 * an undo/redo already carries its model change in the same Yjs step.
 * WHY a microtask and plain `deleteTable` (no stopCapturing): a Yjs write inside `update()`
 * would fire widget observers mid-update; right after it, the model delete still merges into
 * the anchor delete's undo step, so one Cmd+Z restores both (the pairing `deleteTableFromDoc`
 * does in one transaction).
 * WHY re-checked at fire time: a drag-move (delete + insert of the same anchor) and the
 * `cloneDuplicateAnchors` full rewrite remove an anchor that is still in the text.
 */
function dropWholeRemovedModels(u: ViewUpdate): void {
  const removed = new Set<string>();
  for (const tr of u.transactions) {
    if (!tr.docChanged || tr.annotation(ySyncAnnotation)) continue;
    for (const id of wholeRemovedAnchorIds(tr)) removed.add(id);
  }
  if (removed.size === 0) return;
  const view = u.view;
  const doc = entityYdoc(view);
  queueMicrotask(() => {
    // Identity guard (bound at arm time, re-verified here): the view still resolves to
    // THIS doc and is bound to it (its text is the doc's content).
    if (!doc || entityYdoc(view) !== doc) return;
    const live = doc.getText('content').toString();
    if (view.state.doc.toString() !== live) return;
    const tables = getTablesMap(doc);
    for (const id of removed) {
      if (tables.has(id) && !live.includes(`${ANCHOR_MARKER}${id})`)) deleteTable(doc, id);
    }
  });
}

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
        dropWholeRemovedModels(u);

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
