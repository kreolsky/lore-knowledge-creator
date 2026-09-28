/** Editor position persistence: cursor/scroll cache, restore on mount, flush on switch/unload. */
// ARCH: Module-level position cache survives React re-renders, cleared on unmount.
// ARCH: documentPositions uses char offset, not pixel scroll — decorations change line heights.

import { useEffect, useCallback } from 'react';
import { EditorView } from '@codemirror/view';
import { useUIStore, flushUISync } from '../store/ui-store';
import { firstVisibleLine, flushPositionCache, positionCacheGet, positionCacheSet } from '../editor/position-cache';
import type { Document, Reference } from '../types';

type ActiveItem = Document | Reference | null;

interface UseEditorPositionParams {
  editorViewRef: React.RefObject<EditorView | null>;
  activeItemRef: React.RefObject<ActiveItem>;
}

export function useEditorPosition({ editorViewRef, activeItemRef }: UseEditorPositionParams) {
  const getItemId = useCallback((item: ActiveItem) => {
    return item && 'reference_id' in item ? item.reference_id : item?.document_id;
  }, []);

  const restoreScrollPosition = useCallback(() => {
    const view = editorViewRef.current;
    if (!view) return;
    const cur = activeItemRef.current;
    const id = getItemId(cur);
    if (!id) return;
    const pos = positionCacheGet(id)
      || useUIStore.getState().documentPositions[id];
    if (pos && view.state.doc.length > 0) {
      const maxPos = view.state.doc.length;
      // yMargin is the NEGATED pixel offset: a line that was cut by N px above the edge
      // lands N px above it again, and a line that sat below the scroller padding
      // (negative offset) lands below it — the saved scrollTop is reproduced exactly.
      view.dispatch({
        selection: { anchor: Math.min(pos.cursor, maxPos) },
        effects: EditorView.scrollIntoView(Math.min(pos.scroll, maxPos), { y: 'start', yMargin: -(pos.scrollOffset ?? 0) }),
      });
    }
  }, [editorViewRef, activeItemRef, getItemId]);

  useEffect(() => {
    // INVARIANT: order is load-bearing — snapshot live editor state into the
    // cache (bypasses the 500ms editor-observer debounce), then flush cache
    // into Zustand, then ship Zustand to server via keepalive PUT.  Why: the order is load-bearing: snapshot live state (bypassing the 500ms observer debounce) → flush to Zustand → keepalive PUT, so the position ships even during teardown.
    const handler = () => {
      const view = editorViewRef.current;
      const id = getItemId(activeItemRef.current);
      if (view && id) {
        const cursor = view.state.selection.main.head;
        const { from, offset } = firstVisibleLine(view);
        positionCacheSet(id, { cursor, scroll: from, scrollOffset: offset });
      }
      flushPositionCache();
      flushUISync();
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [editorViewRef, activeItemRef, getItemId]);

  // Restoration race: loadProjectPrefs is async, editor may mount before
  // documentPositions[id] arrives from server. Subscribe and fire restoration
  // once when the position appears for the currently active item.
  useEffect(() => {
    const restoredFor = new Set<string>();
    const unsub = useUIStore.subscribe((state) => {
      const id = getItemId(activeItemRef.current);
      if (!id || restoredFor.has(id)) return;
      const pos = state.documentPositions[id];
      const view = editorViewRef.current;
      if (!pos || !view || view.state.doc.length === 0) return;
      restoredFor.add(id);
      // WHY: defer the restore dispatch out of the current call stack.
      // Why: this subscriber can fire synchronously from inside a CM6
      // ViewPlugin.update() (via setSelectionEmpty → setState). Calling
      // view.dispatch() there is re-entrant and CM6 throws
      // "Calls to EditorView.update are not allowed while an update is in progress".
      queueMicrotask(restoreScrollPosition);
    });
    return unsub;
  }, [editorViewRef, activeItemRef, getItemId, restoreScrollPosition]);

  return {
    restoreScrollPosition,
    getItemId,
  };
}
