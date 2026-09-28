/**
 * Selection-region-agent editor wiring — the CM6 half of the pinned-region
 * highlight: the facet resolver (live range), the docChanged auto-unpin
 * listener, and the chat-store signal that re-triggers resolution when a
 * pin/unpin/switch fires NO CM6 transaction.
 *
 * // see SYSTEM: selection-region-agent — editor-side wiring the Editor mounts.
 *
 * makeRegionResolver: stable closure the highlight StateField calls on every
 * CM6 transaction. Reads live chat-store + localStorage + the active handle at
 * CALL time, so it never goes stale. Only highlights when the region's doc_id
 * matches THIS editor's activeItemId (the editor showing the pinned doc).
 * Focused-handle limitation: in split view a non-focused editor showing the
 * pinned doc won't highlight (v1).
 *
 * regionLostListener: reactive auto-unpin when the anchor is lost. A wholesale
 * content replace (checkpoint restore / doc import) arrives as a docChanged
 * transaction that destroys the anchored Yjs items; detect it here so the
 * highlight + pill clear immediately, not only on the next send/apply. Gated
 * on docChanged (skips viewport/focus/selection) and on the region's doc
 * matching THIS editor. regionLost is idempotent while pinned, so concurrent
 * editors on the same doc racing this listener is harmless.
 */
import { useEffect, type RefObject } from 'react';
import type { Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { useChatStore } from '../../store/chat-store';
import type { ChatState } from '../../store/chat-store/types';
import { getPendingRegion, resolveRegion, resolveRegionRange } from '../../store/chat-store/pending-selection';
import { regionChanged, type RegionRange } from '../../editor/region-highlight';

/**
 * Build the stable resolver the region-highlight StateField calls. The
 * `getActiveItemId` callback lets the closure read the editor's live entity id
 * without being a dep of anything.
 */
export function makeRegionResolver(
  getActiveItemId: () => string | undefined,
): () => RegionRange | null {
  return (): RegionRange | null => {
    const store = useChatStore.getState();
    const sessionId = store.activeSessionId;
    const myDocId = getActiveItemId();
    // Ghost pin (activeSessionId === null): the region is held in-memory
    // (store.ghostRegion), NOT localStorage — a single branch key keeps the
    // ghost and materialized resolvers from drifting. Gate on the ghost region's doc_id so
    // only the editor showing the pinned doc highlights.
    if (!sessionId) {
      const ghost = store.ghostRegion;
      if (!ghost || ghost.doc_id !== myDocId) return null;
      return resolveRegionRange(ghost);
    }
    const session = store.sessions.find(s => s.session_id === sessionId);
    if (!session?.has_region) return null;
    const regionDocId = session.target_doc_id ?? session.document_id;
    if (!regionDocId || regionDocId !== myDocId) return null;
    const region = getPendingRegion(sessionId);
    if (!region) return null;
    return resolveRegionRange(region);
  };
}

/**
 * The docChanged auto-unpin listener — see module docstring. Mounted as a
 * plain updateListener next to the highlight extension.
 */
export function regionLostListener(
  getActiveItemId: () => string | undefined,
): Extension {
  return EditorView.updateListener.of((update) => {
    if (!update.docChanged) return;
    const store = useChatStore.getState();
    const sessionId = store.activeSessionId;
    // Ghost pin (activeSessionId === null): the in-memory region has no session to
    // PATCH — a wholesale content replace that destroys its anchor just clears it
    // (mirror of the materialized regionLost). resolveRegionRange returns null only on
    // a lost anchor here (docChanged ⇒ a live ydoc is bound, so it is never 'offline').
    if (!sessionId) {
      const ghost = store.ghostRegion;
      if (!ghost || ghost.doc_id !== getActiveItemId()) return;
      if (resolveRegionRange(ghost) === null) store.clearGhostRegion();
      return;
    }
    const session = store.sessions.find(s => s.session_id === sessionId);
    if (!session?.has_region) return;
    const regionDocId = session.target_doc_id ?? session.document_id;
    if (!regionDocId || regionDocId !== getActiveItemId()) return;
    if (resolveRegion(sessionId).status === 'lost') void store.regionLost(sessionId);
  });
}

/** The active session's id, but only while it holds a pinned region. */
export function selectActiveRegionSessionId(s: ChatState): string | null {
  const id = s.activeSessionId;
  if (!id) return null;
  const sess = s.sessions.find(x => x.session_id === id);
  return sess?.has_region ? id : null;
}

/**
 * Identity key for the ghost pin (no active session). Tracked so a ghost
 * pin/clear — which fires NO CM6 transaction — still re-triggers highlight
 * resolution via the regionChanged annotation.
 */
export function selectGhostRegionKey(s: ChatState): string | null {
  return s.activeSessionId === null && s.ghostRegion
    ? `${s.ghostRegion.doc_id}:${JSON.stringify(s.ghostRegion.relFrom)}:${JSON.stringify(s.ghostRegion.relTo)}`
    : null;
}

/**
 * Re-trigger highlight resolution when the active region changes without a
 * CM6 transaction: subscribe to the active region session and the ghost pin
 * identity, and dispatch a no-op transaction tagged with `regionChanged`.
 * Reads the store directly (the Editor is document-scoped, not chat-scoped)
 * to avoid coupling render to chat-store.
 */
export function useRegionChangedDispatch(
  editorViewRef: RefObject<EditorView | null>,
): void {
  const activeRegionSessionId = useChatStore(selectActiveRegionSessionId);
  const ghostRegionKey = useChatStore(selectGhostRegionKey);
  useEffect(() => {
    // WHY: tag the no-op dispatch with `regionChanged` so the highlight
    // StateField cache gate re-resolves. Without the annotation the cache gate would
    // treat a no-op dispatch as docChanged=false → cache hit → NO refresh, so the
    // highlight would not update on a pin/unpin/switch. The annotation is what forces
    // the re-resolve; a bare dispatch({}) no longer suffices (it used to, when the
    // field rebuilt unconditionally on every transaction).
    editorViewRef.current?.dispatch({ annotations: regionChanged.of(true) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRegionSessionId, ghostRegionKey]);
}
