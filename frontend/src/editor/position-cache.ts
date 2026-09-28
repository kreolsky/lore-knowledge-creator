/**
 * Module-level write-buffer for cursor/scroll positions.
 *
 * ARCH: This is a HIGH-FREQUENCY WRITE-BUFFER, not a plain cache. Writers
 * (editor-observer.ts on scroll/selection-change, useEditorEvents.ts on
 * selection, useEditorPosition.ts) fire every ~200–500ms during interaction.
 * The buffer is drained into the server-persisted
 * `ui-store.documentPositions` only on doc-switch/unload via flushPositionCache().
 *
 * Why module-level (not a useRef or direct ui-store writes):
 *   - A `useRef<Map>` would die on editor unmount (key={activeItemId} remounts
 *     the Editor per document), losing the keyed-by-id LRU and cross-document
 *     persistence that survives entity switches.
 *   - Writing ui-store directly on every event would (a) spam server PUTs via
 *     the ui-store persistence bridge and (b) re-render every
 *     `documentPositions` subscriber on each scroll tick.
 *
 * Conclusion: the module-level Map is the correct tool — embedding the cache
 * into useEditorPosition via a useRef does not work here.
 */
// SYSTEM: position-cache — LRU cursor/scroll position cache (max 200 entries) for editor

import type { EditorView } from '@codemirror/view';
import { useUIStore } from '../store/ui-store';

/** scroll = char offset of the top visible line; scrollOffset = its pixels above the edge (absent in older records → 0). */
export interface EditorPosition { cursor: number; scroll: number; scrollOffset?: number }

const positionCache = new Map<string, EditorPosition>();
const POSITION_CACHE_MAX = 200;

/** Flush all cached positions into Zustand (which persists to server). */
export function flushPositionCache(): void {
  for (const [id, pos] of positionCache) {
    useUIStore.getState().setDocumentPosition(id, pos);
  }
}

/**
 * Drop all cached positions WITHOUT flushing. Why: on logout the next user must not
 * inherit the prior user's cursor/scroll for a same-id document (cross-user stale-state
 * leak). Editor.savedCursor reads positionCacheGet first, so an uncleared cache would
 * place a fresh login at the previous user's position. Mirrors clearLastSavedBlobs.
 */
export function clearPositionCache(): void {
  positionCache.clear();
}

export function positionCacheSet(id: string, pos: EditorPosition): void {
  positionCache.delete(id); // move to end (Map preserves insertion order)
  positionCache.set(id, pos);
  if (positionCache.size > POSITION_CACHE_MAX) {
    const oldest = positionCache.keys().next().value;
    if (oldest !== undefined) positionCache.delete(oldest);
  }
}

export function positionCacheGet(id: string): EditorPosition | undefined {
  return positionCache.get(id);
}

/**
 * The line at the editor's visible top edge, as every writer stores it: `from` is the
 * line's char offset and `offset` how many pixels of it sit above the edge (negative
 * while the scroller's own top padding is still in view).
 */
export function firstVisibleLine(view: EditorView): { from: number; offset: number } {
  // INVARIANT: the height handed to lineBlockAtHeight is scrollTop MINUS the scroller's
  // top padding. Why: CM6 measures heights from the top of .cm-content and only knows
  // .cm-content's own padding, while loreEditorTheme pads .cm-scroller (2rem). Passing raw
  // scrollTop asks for the line 32px BELOW the visible edge; restore then pins that line
  // to the edge, so each reopen of a document drifted one body line upward.
  const paddingTop = parseFloat(getComputedStyle(view.scrollDOM).paddingTop) || 0;
  const edge = view.scrollDOM.scrollTop - paddingTop;
  const block = view.lineBlockAtHeight(Math.max(0, edge));
  return { from: block.from, offset: edge - block.top };
}
