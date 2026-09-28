/** CM6 ViewPlugin that derives live headings from the editor doc and writes them
 * to the app-store `liveHeadings` slice, so the TOC updates in real time.
 *
 * ARCH: ViewPlugin.fromClass (not updateListener.of) because cmExtensions is one
 * memoized array shared by multiple EditorView instances (main editor, secondary
 * split column, snapshot-preview editor). A first-run flag captured in a
 * single updateListener closure would be SHARED across all of them — the first
 * view to mount would consume the first-run, the others never computing their
 * initial headings (breaks the read-only / no-edit case). ViewPlugin.fromClass
 * gives PER-VIEW instance state, so each view computes independently.
 *
 * INVARIANT: never mutate currentDocument/currentReference — liveHeadings is a  Why: liveHeadings is a separate slice so heading updates don't re-render the editor per keystroke; the plugin diffs and only writes when items change.
 * separate slice to avoid re-rendering the editor on every keystroke. The plugin
 * diffs the new items against the last emitted set and only writes when they
 * actually differ, so typing inside a paragraph (no heading change) does not
 * churn the store / re-render the TOC.
 *
 * INVARIANT: per-view `lastItems` is reset on entity switch ONLY because  Why: Editor.tsx remounts on entity switch (key={activeItemId} / snapshot ternary), recreating the plugin, so lastItems resets with the view — no manual reset needed.
 * Editor.tsx mounts the editor with key={activeItemId} (and swaps to/from the
 * snapshot-preview editor via a ternary) — both force a full remount, recreating
 * this plugin instance with an empty lastItems and recomputing in the
 * constructor. Why: if that remount is ever removed (e.g. preserving CM6 state
 * across documents), this diff would carry stale lastItems into the next entity
 * and the TOC could silently miss the first recompute — reset lastItems here.
 */
// SYSTEM: live-headings-extension — derives live headings for the TOC

import type { Extension } from '@codemirror/state';
import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view';
import { extractHeadingsFromLines } from './extract-headings';
import type { HeadingItem } from '../types';
import { useAppStore } from '../store/app-store';

/** Exported for unit tests — the anti-churn diff that gates store writes. */
export function headingsEqual(a: HeadingItem[], b: HeadingItem[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i].level !== b[i].level || a[i].text !== b[i].text || a[i].line !== b[i].line) {
      return false;
    }
  }
  return true;
}

export function liveHeadingsExtension(
  getEntityId: () => string | undefined,
  getEnabled: () => boolean,
): Extension {
  return ViewPlugin.fromClass(
    class {
      private view: EditorView;
      private lastItems: HeadingItem[] = [];

      constructor(view: EditorView) {
        this.view = view;
        // Compute once on mount so read-only / CRDT-synced-without-edits content
        // is still reflected in the TOC (constructor runs before any update()).
        this.compute();
      }

      update(u: ViewUpdate): void {
        if (!u.docChanged) return;
        this.compute();
      }

      private compute(): void {
        if (!getEnabled()) return;
        const entityId = getEntityId();
        if (!entityId) return;
        // Iterate the live Text in O(1) per line via doc.line(i) — avoids a
        // full-doc toString()+split() reallocation on every keystroke.
        const doc = this.view.state.doc;
        const items = extractHeadingsFromLines(doc.lines, i => doc.line(i + 1).text);
        if (headingsEqual(items, this.lastItems)) return;
        this.lastItems = items;
        useAppStore.getState().setLiveHeadings(entityId, items);
      }
    },
  );
}
