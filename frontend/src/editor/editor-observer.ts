/**
 * Unified editor update observer — ViewPlugin with cleanup.
 *
 * ARCH: ViewPlugin.define holds all debounce timers (doc-changed, position-save,
 * scroll-save) in closure scope. destroy() clears all timers + removes scroll
 * listener — no phantom emits after EditorView unmount.
 *
 * Listener #6 in CodeMirrorEditor.tsx (syntax tree readiness, one-shot) remains
 * separate — it's dynamically appended and auto-deregisters after first trigger.
 */
// SYSTEM: editor-observer — unified CM6 update dispatch (replaces 5 updateListeners)

import type React from 'react';
import type { Extension } from '@codemirror/state';
import { ViewPlugin, type ViewUpdate } from '@codemirror/view';
import { takesNotesWithoutFocus } from './note-surface';
import { useUIStore } from '../store/ui-store';
import { firstVisibleLine, positionCacheSet } from './position-cache';
import { emit } from '../events';
import { getEditorView } from './active-editor';
import { transcludeMap } from '../components/editor/live-preview';
import { parseTarget } from '../components/editor/live-preview/transclusion-grammar';

type ActiveItem = { reference_id: string } | { document_id: string } | null;

interface ObserverDeps {
  activeItemRef: React.RefObject<ActiveItem>;
  refIsEmptySetterRef: React.RefObject<(empty: boolean) => void>;
}

function getItemId(item: ActiveItem): string | undefined {
  return item && 'reference_id' in item
    ? item.reference_id
    : (item as { document_id?: string } | null)?.document_id;
}

/**
 * Feed the selection toolbar: whether this view holds a live, non-empty selection.
 *
 * WHY the note-surface split: a view the user cannot type into (a commentator's
 * editor, the public page) is not focusable — `contenteditable=false` takes no focus,
 * so `hasFocus` stays false while the user selects in it with the mouse. Requiring
 * focus there kept the note-only toolbar from ever appearing.
 */
function publishSelection(update: ViewUpdate): void {
  if (!update.selectionSet && !update.focusChanged) return;
  const { view } = update;
  const live = view.hasFocus || takesNotesWithoutFocus(view.state);
  // WHY: the store is one slot shared by every view. A view that is neither live nor the
  // active editor (the document after focus moved into a table cell — its focus loss
  // arrives 10ms late) must not overwrite what the active one published.
  if (!live && getEditorView() !== view) return;
  const { from, to } = view.state.selection.main;
  const empty = from === to || !live;
  useUIStore.getState().setSelectionEmpty(empty);
  if (update.selectionSet && !empty) emit('editor-selection-change', { empty: false });
}

/** The ONE selection feed every active-able editor carries — mounted by `editorExperience`
 * (render-bundle) for the document and the editable cell; `setFocusedView` checks for it. */
export const selectionFeedPlugin = ViewPlugin.define(() => ({ update: publishSelection }));

/** An editable table cell's share of `editor-doc-changed`. WHY: cell text lives in the
 * table model, not in the host view, so the host observer never sees a cell edit — an
 * image dropped only into a cell was never re-scanned for its reference and stayed raw. */
export const cellDocChangedFeed = ViewPlugin.define(() => {
  let timer: ReturnType<typeof setTimeout> | undefined;
  return {
    update(update: ViewUpdate) {
      if (!update.docChanged) return;
      clearTimeout(timer);
      timer = setTimeout(() => emit('editor-doc-changed'), 500);
    },
    destroy() { clearTimeout(timer); },
  };
});

export function createObserverExtension(deps: ObserverDeps): Extension {
  return ViewPlugin.define((view) => {
    let docChangedTimer: ReturnType<typeof setTimeout> | undefined;
    let positionSaveTimer: ReturnType<typeof setTimeout> | undefined;
    let scrollSaveTimer: ReturnType<typeof setTimeout> | undefined;

    const onScroll = () => {
      clearTimeout(scrollSaveTimer);
      scrollSaveTimer = setTimeout(() => {
        const cur = deps.activeItemRef.current;
        const id = getItemId(cur);
        if (!id) return;
        const { from, offset } = firstVisibleLine(view);
        positionCacheSet(id, { cursor: view.state.selection.main.head, scroll: from, scrollOffset: offset });
      }, 200);
    };

    view.scrollDOM.addEventListener('scroll', onScroll);

    return {
      update(update: ViewUpdate) {
        if (update.selectionSet) {
          clearTimeout(positionSaveTimer);
          positionSaveTimer = setTimeout(() => {
            const cur = deps.activeItemRef.current;
            const id = getItemId(cur);
            if (!id) return;
            const cursor = update.view.state.selection.main.head;
            const { from, offset } = firstVisibleLine(update.view);
            positionCacheSet(id, { cursor, scroll: from, scrollOffset: offset });
          }, 500);

        }

        if (update.docChanged) {
          const item = deps.activeItemRef.current;
          if (item && 'reference_id' in item) {
            deps.refIsEmptySetterRef.current(update.view.state.doc.length === 0);
          }

          clearTimeout(docChangedTimer);
          docChangedTimer = setTimeout(() => {
            emit('editor-doc-changed');

            const text = update.view.state.doc.toString();
            // see SYSTEM: transclusion — scan for unresolved embed ids. Covers ref:/doc:/bare-id
            // forms; the id is extracted via the shared `parseTarget` grammar (single
            // source of truth). An id is resolved if it's in the unified transcludeMap.
            // Emits the existing unresolved-image-ref event, which re-fetches references
            // (and re-populates the map).
            const regex = /!\[[^\]]*\]\(([^)]+)\)/g;
            let m;
            while ((m = regex.exec(text)) !== null) {
              const parsed = parseTarget(m[1]);
              if (!parsed) continue; // non-transclusion target (http/data/…)
              if (!transcludeMap.has(parsed.id)) {
                emit('unresolved-image-ref');
                break;
              }
            }
          }, 500);
        }
      },

      destroy() {
        view.scrollDOM.removeEventListener('scroll', onScroll);
        clearTimeout(docChangedTimer);
        clearTimeout(positionSaveTimer);
        clearTimeout(scrollSaveTimer);
      },
    };
  });
}
