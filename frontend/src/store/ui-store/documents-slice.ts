/** Documents slice — per-document right-panel + main-entity UI state (project-persisted).

# ARCH (debt-paydown W6): fifth + final ui-store slice. Owns the per-document UI map
# (documents: Record<docId, DocumentUIState>) + the DocumentUIState / MainEntity / RightTab
# types + DEFAULT_DOC_STATE + the ORPHAN_KEY fallback + _patchDoc + 12 per-doc actions.
# Persisted via get().saveUIStateNow() (not the module-level triggerSaveUI), so the slice
# closes over only set/get — no import of ui-store's persistence core (no circular dep).
# getActiveDocState + useDocState stay in ui-store.ts: they reach for _appBridge /
# useUIStore (ui-store-local), importing ORPHAN_KEY + DEFAULT_DOC_STATE from here.
#
# INVARIANT: rightPanelTab NEVER stores 'search'. The search tab is an ephemeral overlay
# tracked by the top-level searchTabDocId field (set/cleared by this slice, cleared on
# doc switch by app-store, never persisted — buildUIBlob is field-by-field). Why: a
# stored 'search' tab made search the document's remembered tab, so returning to the
# doc (or F5) resurrected search over the user's real saved tab.
*/
import type { UIState } from '../ui-store';

export type RightTab = 'notes' | 'refs' | 'checkpoint' | 'chat' | 'search' | 'links' | 'settings' | 'find' | 'access';
export type RefOpenMode = 'center' | 'split' | 'panel';
export type MainEntity = { type: 'document'; id: string } | { type: 'reference'; id: string };

export interface DocumentUIState {
  // WHY: mainEntity drives what the LEFT (main) area shows. When type='reference',
  // the document is "open" but the reference replaces the editor view. Why: keeps
  // the doc's right-panel/scroll state alive while a non-editable reference holds the main stage.
  mainEntity: MainEntity;
  rightPanelOpen: boolean;
  rightPanelTab: RightTab | null;
  selectedReferenceId?: string | null; // entity for 'refs' tab (independent of mainEntity)
  // 'center' — the reference replaces the document in the center (default);
  // 'split' — document + reference as two columns; 'panel' — the reference opens
  // inside the right panel's Refs tab, the center keeps the document.
  // INVARIANT(persisted): the reference open mode is persisted per-document, like
  // rightPanelTab — each (user × document) remembers its own mode.
  // Why: a user reading one doc side-by-side with its ref shouldn't force that
  // layout onto every other doc.
  refOpenMode?: RefOpenMode;
  // Legacy boolean predecessor of refOpenMode, still present in already-stored
  // `documents` maps. Never written any more; dropped on the first setRefOpenMode write.
  // INVARIANT(persisted): read ONLY through readRefOpenMode, where splitView:true = 'split'.
  // Why: user_preferences persists the map wholesale, so a doc with splitView:true
  // stored before the tristate existed must still open in split.
  splitView?: boolean;
  // ARCH: per-document opt-in to rank
  // the chat list by tree-distance to THIS document (proximity) instead of the
  // global recency order. Persisted like refOpenMode (the whole `documents` map is
  // saved wholesale to user_preferences, so a new field auto-persists). Default
  // OFF → recency. Why per-doc: proximity is only meaningful relative to an open
  // doc, and the choice should not leak across unrelated documents.
  chatSortByProximity?: boolean;
}

// INVARIANT: frozen so the same reference can safely be returned from selectors.
// Why: freshly-allocated literals would break Zustand equality checks and cause
// "Maximum update depth exceeded" loops. mainEntity.id is intentionally empty;
// useDocState consumers read rightPanelTab/rightPanelOpen, not mainEntity.id.
export const DEFAULT_DOC_STATE: DocumentUIState = Object.freeze({
  mainEntity: Object.freeze({ type: 'document' as const, id: '' }) as MainEntity,
  rightPanelOpen: true,
  // WHY: a document the user never opened a tab on starts on Chat (operator's default);
  // a role without chat falls back to its first allowed tab (ProjectPage allowedRightTabs).
  rightPanelTab: 'chat' as RightTab | null,
}) as DocumentUIState;

// Orphan (no-active-doc) right-panel state lives under documents['__orphan__']. Storing
// it in Zustand state (not module-scope) lets selectors return stable references and
// avoids re-render loops. Exported because ui-store's stripOrphan / getActiveDocState /
// useDocState still reference it.
export const ORPHAN_KEY = '__orphan__';

/** The ONE reader of a doc entry's reference open mode — folds the legacy splitView flag
 *  and the compact viewport. `compact` is required so every reader states it. */
export function readRefOpenMode(entry: DocumentUIState | undefined, compact: boolean): RefOpenMode {
  // INVARIANT: on a compact viewport the second window opens in the center; the
  // stored mode is untouched.
  // Why: operator decision — a phone has one window; desktop keeps its own layout.
  if (compact) return 'center';
  // INVARIANT(persisted): refOpenMode wins; a legacy `splitView: true` with no
  // refOpenMode reads as 'split'.
  // Why: already-stored preferences carry the boolean; they must keep opening in split.
  return entry?.refOpenMode ?? (entry?.splitView ? 'split' : 'center');
}

/** Document AND reference visible together — 'split' (two columns) and 'panel'
 *  (reference inside the Refs tab). The ONE predicate behind ghost-chat context,
 *  note attachment and the chat-context bridge: every "is split" reader goes through
 *  it, so a fourth mode is classified once, here. */
export function showsBothPanes(mode: RefOpenMode): boolean {
  return mode !== 'center';
}

/** Does the open reference take part in the DOCUMENT's scope (nav, tree, TOC,
 *  notes, chat, header)? 'panel' is a QUICK PREVIEW: the user stays in the
 *  document and the reference is a viewer that takes part in nothing but its
 *  own tab. The ONE projection — every `currentReference` consumer that infers
 *  "the reference is what is open" reads it through this
 *  (`refIsScope(mode) ? currentReference : null`); no consumer branches on
 *  'panel' by hand. Why one function: six+ sites carry the same rule, a
 *  per-site literal drifts. */
export function refIsScope(mode: RefOpenMode): boolean {
  return mode !== 'panel';
}

export type DocumentsSlice = Pick<
  UIState,
  | 'documents' | 'searchTabDocId' | 'clearSearchTabOverlay'
  | 'setRightPanelTab' | 'setRightPanelOpen' | 'setMainEntity' | 'getDocState'
  | 'removeDocState' | 'removeDocStates' | 'setCurrentReferenceForDoc' | 'getCurrentReferenceForDoc'
  | 'setRefOpenMode' | 'getRefOpenMode' | 'pinRightPanel'
  | 'setChatSortByProximity' | 'getChatSortByProximity'
>;

type UISet = (
  partial: Partial<UIState> | ((state: UIState) => Partial<UIState>),
) => void;
type UIGet = () => UIState;

function _patchDoc(get: UIGet, set: UISet, docId: string, patch: Partial<DocumentUIState>, persist = true) {
  const prev = get().documents;
  const existing = prev[docId] ?? { ...DEFAULT_DOC_STATE, mainEntity: { type: 'document', id: docId } };
  const next = { ...existing, ...patch };
  set({ documents: { ...prev, [docId]: next } });
  if (persist) get().saveUIStateNow();
}

export function createDocumentsSlice(set: UISet, get: UIGet): DocumentsSlice {
  return {
    documents: {},
    searchTabDocId: null,
    setRightPanelTab: (docId, tab) => {
      // INVARIANT (see file header): 'search' is an overlay, never a stored tab —
      // set the flag, touch no doc slice, fire no save. Every other tab selection
      // clears a live overlay and stores normally.
      if (tab === 'search') {
        set({ searchTabDocId: docId ?? ORPHAN_KEY });
        return;
      }
      set({ searchTabDocId: null });
      // Orphan slice is not persisted — pass persist=false.
      _patchDoc(get, set, docId ?? ORPHAN_KEY, { rightPanelTab: tab }, /*persist*/ !!docId);
    },
    setRightPanelOpen: (docId, open) => {
      // Closing the panel kills a live search overlay (reopening restores the
      // stored tab — the "last opened tab" semantics of the toggle-close path).
      if (!open) set({ searchTabDocId: null });
      _patchDoc(get, set, docId ?? ORPHAN_KEY, { rightPanelOpen: open }, /*persist*/ !!docId);
    },
    clearSearchTabOverlay: () => set({ searchTabDocId: null }),
    setMainEntity: (docId, entity) => {
      _patchDoc(get, set, docId, { mainEntity: entity });
    },
    getDocState: (docId) => {
      return get().documents[docId ?? ORPHAN_KEY] ?? DEFAULT_DOC_STATE;
    },
    removeDocState: (docId) => {
      const prev = get().documents;
      if (!(docId in prev)) return;
      const next = { ...prev };
      delete next[docId];
      set({ documents: next });
      get().saveUIStateNow();
    },
    removeDocStates: (docIds) => {
      const prev = get().documents;
      let changed = false;
      const next = { ...prev };
      for (const id of docIds) {
        if (id in next) {
          delete next[id];
          changed = true;
        }
      }
      if (changed) {
        set({ documents: next });
        get().saveUIStateNow();
      }
    },
    setCurrentReferenceForDoc: (docId, refId) => {
      _patchDoc(get, set, docId, { selectedReferenceId: refId ?? null });
    },
    getCurrentReferenceForDoc: (docId) => get().documents[docId]?.selectedReferenceId ?? null,
    setRefOpenMode: (docId, mode) => {
      // The legacy flag is dropped on the first write so it can never resurrect a
      // 'split' over a later 'center' (readRefOpenMode prefers refOpenMode anyway;
      // deleting keeps the persisted entry clean).
      const prev = get().documents;
      const existing = prev[docId] ?? { ...DEFAULT_DOC_STATE, mainEntity: { type: 'document' as const, id: docId } };
      const { splitView: _legacy, ...rest } = existing;
      void _legacy;
      set({ documents: { ...prev, [docId]: { ...rest, refOpenMode: mode } } });
      get().saveUIStateNow();
    },
    getRefOpenMode: (docId) => readRefOpenMode(get().documents[docId], get().compactLayout),
    // Pin the right panel OPEN on `tab` for a document the user is about to navigate to.
    // The panel state is per-document (ProjectShell reads `documents[id].rightPanelTab /
    // rightPanelOpen`), so a "go to parent, keep X open" jump must write the TARGET doc's
    // entry before the switch — otherwise the target's remembered tab wins and X is gone.
    pinRightPanel: (docId, tab) => {
      _patchDoc(get, set, docId, { rightPanelTab: tab, rightPanelOpen: true });
    },
    setChatSortByProximity: (docId, value) => {
      _patchDoc(get, set, docId, { chatSortByProximity: value });
    },
    getChatSortByProximity: (docId) => get().documents[docId ?? '']?.chatSortByProximity ?? false,
  };
}
