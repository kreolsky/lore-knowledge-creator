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
import { useUIStore } from '../store/ui-store';
import { firstVisibleLine, positionCacheSet } from './position-cache';
import { emit } from '../events';
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

          const { from, to } = update.view.state.selection.main;
          const empty = from === to || !update.view.hasFocus;
          useUIStore.getState().setSelectionEmpty(empty);
          if (!empty) {
            emit('editor-selection-change', { empty: false });
          }
        }

        if (update.focusChanged && !update.selectionSet) {
          const { from, to } = update.view.state.selection.main;
          const empty = from === to || !update.view.hasFocus;
          useUIStore.getState().setSelectionEmpty(empty);
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
              if (!parsed) continue; // non-transclusion target (http/data/note:…)
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
