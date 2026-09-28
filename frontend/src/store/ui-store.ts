/** Zustand store for UI state — panels, theme, language, preferences persistence. */
// ARCH: All UI state persisted server-side in user_preferences table (SurrealDB).
// ARCH: composed from slices (ui-store/*-slice.ts) following the chat-store pattern — the
//       store file owns only persistence + the documents map; each feature area is a slice.
//       The former "580-line god store" DEBT was paid down in the debt-paydown W6 round; the
//       selector-migration risk it deferred on was handled by keeping every action name and
//       state key identical across the split (no consumer changed).
// ARCH: Per-project state: PUT /api/preferences/{projectId} (debounced).
// ARCH: Global state (theme, panel widths): PUT /api/preferences/_global (immediate).
// ARCH: Right-panel state (tab, open/closed) + per-tab entity (chat session, selected reference)
//       and main-area entity (document or reference) live PER-DOCUMENT in `documents` map,
//       so each (user × document) keeps its own UI layout. See DocumentUIState below.
// WHY: In-memory lastSavedBlobs cache prevents cross-project contamination in buildUIBlob.
// WHY: documentPositions uses char offset, not pixel scroll — decorations change line heights.
// ARCH: Uses bridge pattern (registerAppBridge) instead of direct import to avoid circular dep with app-store.
// SYSTEM: ui-store — panel state, theme, language, preferences persistence

import { create } from 'zustand';
import { Language, SearchResult } from '../types';
import { apiClient } from '../api/client';
import type { EditorPosition } from '../editor/position-cache';
import { createPublicShareSlice } from './ui-store/public-share-slice';
import { createSearchSlice } from './ui-store/search-slice';
import { createSidebarSlice } from './ui-store/sidebar-slice';
import { createThemeSlice, type GlobalPrefs, type AdminSectionTab } from './ui-store/theme-slice';
export type { AdminSectionTab };
import { createDocumentsSlice, DEFAULT_DOC_STATE, ORPHAN_KEY, readRefOpenMode, showsBothPanes } from './ui-store/documents-slice';
import type { RightTab, MainEntity, DocumentUIState, RefOpenMode } from './ui-store/documents-slice';

export { DEFAULT_DOC_STATE, ORPHAN_KEY, readRefOpenMode, showsBothPanes };
export type { RightTab, MainEntity, DocumentUIState, RefOpenMode };

interface PersistedUIState {
  sidebarTab: 'docs' | 'toc';
  sidebarOpen: boolean;
  lastDocId?: string;
  lastDocPosition?: EditorPosition;
  collapsedDocIds?: string[];
  // Per-document UI slice — single source of truth for right-panel state
  // and main-area entity per (user × document).
  documents?: Record<string, DocumentUIState>;
  // Project-level active AI chat. ONE chat is
  // remembered active per (user × project) and survives document navigation.
  lastActiveChatSessionId?: string | null;
  // Last target chosen in the Access tab's "move to another project" section.
  lastMoveTargetProjectId?: string | null;
  searchQuery?: string;
  searchMode?: 'fulltext' | 'semantic';
}

export const UI_DEFAULTS: PersistedUIState = {
  sidebarTab: 'docs',
  sidebarOpen: true,
  collapsedDocIds: [],
  documents: {},
  lastActiveChatSessionId: null,
};

const lastSavedBlobs = new Map<string, PersistedUIState>();
// In-flight loadProjectPrefs promises so callers (e.g. setCurrentDocument on F5)
// can await prefs hydration before reading per-doc slices.
// INVARIANT: also serves as a dedup gate — loadProjectPrefs returns early when an
// entry exists. Why: StrictMode double-mount / repeated setCurrentProject otherwise
// fire duplicate GET /api/preferences/{projectId} for a single open.
const _projectPrefsPromises = new Map<string, Promise<void>>();

interface AppContext {
  currentUser: { user_id: string } | null;
  currentProject: { project_id: string } | null;
  currentDocument: { document_id: string } | null;
}

let _appBridge: {
  getAppContext: () => AppContext;
  showToast: (msg: string, type: 'info' | 'error') => void;
} | null = null;

export function registerAppBridge(bridge: {
  getAppContext: () => AppContext;
  showToast: (msg: string, type: 'info' | 'error') => void;
}) {
  _appBridge = bridge;
}

function stripOrphan(docs: Record<string, DocumentUIState>): Record<string, DocumentUIState> {
  if (!(ORPHAN_KEY in docs)) return docs;
  const next = { ...docs };
  delete next[ORPHAN_KEY];
  return next;
}

function buildUIBlob(uiState: UIState, appCtx: AppContext): PersistedUIState | null {
  const userId = appCtx.currentUser?.user_id;
  const projectId = appCtx.currentProject?.project_id;
  if (!userId || !projectId) return null;

  const cacheKey = `${userId}:${projectId}`;
  const existing = lastSavedBlobs.get(cacheKey) ?? { ...UI_DEFAULTS };
  const docId = appCtx.currentDocument?.document_id;
  const currentPos = docId ? uiState.documentPositions[docId] : undefined;

  return {
    sidebarTab: uiState.sidebarTab,
    sidebarOpen: uiState.sidebarOpen,
    lastDocId: currentPos ? docId : existing.lastDocId,
    lastDocPosition: currentPos ?? existing.lastDocPosition,
    collapsedDocIds: uiState.collapsedDocIds,
    // Strip the ephemeral orphan slice from the persisted blob.
    documents: stripOrphan(uiState.documents),
    lastActiveChatSessionId: uiState.lastActiveChatSessionId,
    lastMoveTargetProjectId: uiState.lastMoveTargetProjectId,
    searchQuery: uiState.searchQuery,
    // ARCH: per-session context lives on chat_sessions in DB now (synced via immediate PATCH).
    // Intentionally dropped from the UI-prefs blob so the server-side row is the single
    // source of truth and stale prefs entries get overwritten.
    searchMode: uiState.searchMode,
  };
}

let _saveUITimeout: ReturnType<typeof setTimeout> | null = null;
let _lastPrefToastAt = 0;

function triggerSaveUI(uiState: UIState) {
  if (!_appBridge) return;
  // INVARIANT: never persist while the current project's prefs are un-hydrated.
  // Why: resetForProjectSwitch wipes `documents`/`collapsedDocIds` to empty and sets
  // projectPrefsLoaded=false; a save firing in that window (setMainEntity on doc commit,
  // editor position, sidebar hotkey) snapshots the empty state, and the last-writer-wins
  // UPSERT clobbers the server row — then every later hydration legitimately loads the
  // emptied blob (cascade). Observed: right-panel tab reverts to 'refs', tree expands fully.
  // See plans/investigate-prefs-state-resets.md.
  if (!uiState.projectPrefsLoaded) return;
  const appCtx = _appBridge.getAppContext();
  const userId = appCtx.currentUser?.user_id;
  const projectId = appCtx.currentProject?.project_id;
  if (!userId || !projectId) return;

  const blob = buildUIBlob(uiState, appCtx);
  if (!blob) return;

  lastSavedBlobs.set(`${userId}:${projectId}`, blob);

  if (_saveUITimeout) clearTimeout(_saveUITimeout);
  _saveUITimeout = setTimeout(() => {
    apiClient.put(`/preferences/${projectId}`, { preferences: blob })
      .catch(() => {
        const now = Date.now();
        if (!_lastPrefToastAt || now - _lastPrefToastAt > 30000) {
          _lastPrefToastAt = now;
          _appBridge?.showToast('Settings not saved', 'error');
        }
      });
  }, 200);
}

// WHY: 200ms debounce isn't enough for fast cmd+shift+r — cancelled timer means last
// cursor position / panel tab is lost. keepalive guarantees delivery on unload.
export function flushUISync() {
  if (!_appBridge) return;
  // INVARIANT: same hydration gate as triggerSaveUI — an unload during the un-hydrated
  // window must not keepalive-PUT an empty blob and clobber the server row. Why: see
  // triggerSaveUI above.
  if (!useUIStore.getState().projectPrefsLoaded) return;
  const appCtx = _appBridge.getAppContext();
  const userId = appCtx.currentUser?.user_id;
  const projectId = appCtx.currentProject?.project_id;
  if (!userId || !projectId) return;

  const blob = buildUIBlob(useUIStore.getState(), appCtx);
  if (!blob) return;

  if (_saveUITimeout) { clearTimeout(_saveUITimeout); _saveUITimeout = null; }
  lastSavedBlobs.set(`${userId}:${projectId}`, blob);

  fetch(`/api/preferences/${projectId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ preferences: blob }),
    keepalive: true,
  }).catch(() => { /* unload — toast won't render */ });
}

function triggerSaveGlobalPrefs(state: UIState) {
  if (!_appBridge) return;
  // INVARIANT: public-share (anonymous /s/:token) MUST NOT reach /preferences/_global.
  // Why: that route is auth-gated (Depends(get_current_user)) and returns 401 for
  // anonymous visitors; handleResponse turns 401 into window.location.href='/' and
  // kicks the visitor off the share page on every theme/lang/resize interaction.
  // Theme/language/panelWidth/splitRatio still apply in-session via their setters
  // (they write store state before calling this function); only persistence is skipped.
  if (state.isPublicShare) return;
  const blob: GlobalPrefs = {
    theme: state.theme,
    panelWidths: state.panelWidths,
    language: state.language,
    splitRatio: state.splitRatio,
    showArchived: state.showArchived,
    adminSectionTab: state.adminSectionTab,
  };
  apiClient.put('/preferences/_global', { preferences: blob })
    .catch(() => _appBridge?.showToast('Settings not saved', 'error'));
}

export function clearLastSavedBlobs() {
  lastSavedBlobs.clear();
  // Also cancel any pending debounced save — used by tests to prevent timer
  // leakage from one test polluting the next test's spy.
  if (_saveUITimeout) { clearTimeout(_saveUITimeout); _saveUITimeout = null; }
}

export interface UIState {
  sidebarTab: 'docs' | 'toc';
  sidebarOpen: boolean;
  collapsedDocIds: string[];
  panelWidths: { left: number | null; right: number | null };
  splitRatio: number;
  theme: 'light' | 'dark';
  language: Language;
  // Global "Show archived" reference-panel filter (global-persisted).
  showArchived: boolean;
  // Admin panel's last-open section (global-persisted per user; see
  // theme-slice GlobalPrefs). The aside WIDTH is not a section field: it is
  // panelWidths.left — ONE width shared with the project sidebar (no jump in
  // either direction; see hooks/useSectionAsideWidth.ts).
  adminSectionTab: AdminSectionTab | null;
  globalPrefsLoaded: boolean;
  // WHY: false until the current project's prefs have hydrated. The right
  // panel renders a loading state (not content) while false. Why: DEFAULT_DOC_STATE
  // would otherwise mount the default tab's panel — a wasted load and a visible
  // flick to the real saved tab on hydration.
  projectPrefsLoaded: boolean;
  documents: Record<string, DocumentUIState>;
  // Ephemeral search-tab overlay: the docId (or ORPHAN_KEY) whose right panel is
  // currently showing 'search'. 'search' is never a stored documents[].rightPanelTab
  // value — it lives only here (see documents-slice.ts header INVARIANT + Why), and
  // buildUIBlob (field-by-field) keeps it out of the persisted blob. Cleared on panel
  // close, other-tab select, doc switch (app-store setCurrentDocument) and project switch.
  searchTabDocId: string | null;
  searchQuery: string;
  searchResults: SearchResult[];
  searchMode: 'fulltext' | 'semantic';
  documentPositions: Record<string, EditorPosition>;
  selectionEmpty: boolean;
  // Project-level active AI chat id.
  // Persisted in the project prefs blob; survives document navigation + reload.
  lastActiveChatSessionId: string | null;
  // Per-project memory of the move section's target project (persisted blob).
  lastMoveTargetProjectId: string | null;
  // WHY: Transient one-shot seed for FindReplacePanel. Set on every Cmd+F
  // (carries the editor selection) and read once on panel mount to bridge the
  // fresh-open timing gap (the panel subscribes to open-find too late). NOT persisted.
  findSeedText: string;

  setSidebarTab: (tab: 'docs' | 'toc') => void;
  setSidebarOpen: (open: boolean) => void;
  // Per-document right-panel actions
  setRightPanelTab: (docId: string | null, tab: RightTab | null) => void;
  setRightPanelOpen: (docId: string | null, open: boolean) => void;
  clearSearchTabOverlay: () => void;
  setMainEntity: (docId: string, entity: MainEntity) => void;
  getActiveDocState: () => DocumentUIState;
  getDocState: (docId: string | null | undefined) => DocumentUIState;
  removeDocState: (docId: string) => void;
  removeDocStates: (docIds: string[]) => void;
  setDocumentPosition: (docId: string, pos: EditorPosition) => void;
  toggleDocExpanded: (docId: string) => void;
  // Reveal-in-tree: expand a set of ancestors at once (only expands, never collapses).
  expandDocs: (docIds: string[]) => void;
  setSelectionEmpty: (v: boolean) => void;
  setFindSeed: (text: string) => void;
  setLastActiveChatSession: (id: string | null) => void;
  getLastActiveChatSession: () => string | null;
  setLastMoveTargetProject: (id: string | null) => void;
  setCurrentReferenceForDoc: (docId: string, refId: string | null) => void;
  getCurrentReferenceForDoc: (docId: string) => string | null;
  setSearchCache: (query: string, results: SearchResult[]) => void;
  setRefOpenMode: (docId: string, mode: RefOpenMode) => void;
  getRefOpenMode: (docId: string) => RefOpenMode;
  pinRightPanel: (docId: string, tab: RightTab) => void;
  setChatSortByProximity: (docId: string, value: boolean) => void;
  getChatSortByProximity: (docId: string | null | undefined) => boolean;
  setSplitRatio: (ratio: number) => void;
  getSplitRatio: () => number;
  setShowArchived: (showArchived: boolean) => void;
  setAdminSectionTab: (tab: AdminSectionTab) => void;
  setSearchMode: (mode: 'fulltext' | 'semantic') => void;
  setTheme: (theme: 'light' | 'dark') => void;
  setLanguage: (language: Language) => void;
  setPanelWidth: (side: 'left' | 'right', width: number) => void;
  loadGlobalPrefs: () => Promise<void>;
  saveUIStateNow: () => void;
  saveGlobalPrefsNow: () => void;
  applyProjectPrefs: (serverPrefs: Partial<PersistedUIState>) => void;
  loadProjectPrefs: (userId: string, projectId: string) => void;
  awaitProjectPrefs: (projectId: string) => Promise<void>;
  resetForProjectSwitch: (isProjectSwitch: boolean) => void;
  // PUBLIC SHARE — true only while /s/:token is mounted. Why a store field (not a
  // page-local prop): gating hooks deep in Editor/Sidebar/ReferencesPanel/Header
  // need read access without prop-drilling. The public page sets it on mount,
  // clears on unmount. Pairs with `accessLevel='readonly'` (the access gate) and
  // the page-level `setPublicFileContext(token)` (the file-URL gate).
  isPublicShare: boolean;
  setPublicShare: (v: boolean) => void;
  // Owning project's name on the anonymous surface — the public Header breadcrumb
  // root. Set after the public tree loads, cleared on unmount. Null off /s/:token.
  publicProjectName: string | null;
  setPublicProjectName: (name: string | null) => void;
  // WHY: a signed-in visitor whose document read was REFUSED (404) on a
  // /docs/<id> URL is served the anonymous published view for that id instead of
  // being bounced. Why: "шара должна быть доступна для всех" — a published link
  // opens for everyone, and holding a session for another project is not a reason
  // to see less than a logged-out visitor. Ids accumulate: the refused id plus
  // every id of the published tree it resolves to, so navigating inside a shared
  // subtree does not re-run the refused authed fetch.
  publicFallbackDocIds: Set<string>;
  markPublicFallback: (ids: string[]) => void;
}

export const useUIStore = create<UIState>((set, get) => ({
  ...createSidebarSlice(set, get),
  ...createThemeSlice(set, get),
  projectPrefsLoaded: false,
  ...createDocumentsSlice(set, get),
  ...createSearchSlice(set, get),
  documentPositions: {},
  selectionEmpty: true,
  lastActiveChatSessionId: null,
  lastMoveTargetProjectId: null,
  findSeedText: '',
  ...createPublicShareSlice(set),

  getActiveDocState: () => {
    const docId = _appBridge?.getAppContext().currentDocument?.document_id ?? ORPHAN_KEY;
    return get().documents[docId] ?? DEFAULT_DOC_STATE;
  },
  setDocumentPosition: (docId, pos) => {
    set(prev => ({
      documentPositions: { ...prev.documentPositions, [docId]: pos }
    }));
    triggerSaveUI(get());
  },
  setSelectionEmpty: (v) => set({ selectionEmpty: v }),
  setFindSeed: (text) => set({ findSeedText: text }),
  setLastActiveChatSession: (id) => {
    set({ lastActiveChatSessionId: id });
    triggerSaveUI(get());
  },
  getLastActiveChatSession: () => get().lastActiveChatSessionId ?? null,
  setLastMoveTargetProject: (id) => {
    set({ lastMoveTargetProjectId: id });
    triggerSaveUI(get());
  },

  saveUIStateNow: () => {
    triggerSaveUI(get());
  },
  saveGlobalPrefsNow: () => {
    triggerSaveGlobalPrefs(get());
  },
  applyProjectPrefs: (serverPrefs) => {
    const merged = { ...UI_DEFAULTS, ...serverPrefs };
    // WHY: Migrate persisted 'info' tab → 'checkpoint' (Info tab removed).
    // WHY: Migrate persisted 'search' tab → 'refs' (search is an ephemeral overlay
    // now; legacy rows saved it as the stored tab).
    if (merged.documents) {
      for (const docId of Object.keys(merged.documents)) {
        const ds = merged.documents[docId];
        if (ds.rightPanelTab === 'info' as RightTab | null) {
          ds.rightPanelTab = 'checkpoint';
        }
        if (ds.rightPanelTab === 'search') {
          ds.rightPanelTab = 'refs';
        }
      }
    }
    const serverDocPositions: Record<string, EditorPosition> = {};
    if (merged.lastDocPosition && merged.lastDocId) {
      serverDocPositions[merged.lastDocId] = merged.lastDocPosition;
    }
    set({
      sidebarTab: merged.sidebarTab,
      sidebarOpen: merged.sidebarOpen,
      collapsedDocIds: merged.collapsedDocIds ?? [],
      documents: merged.documents ?? {},
      lastActiveChatSessionId: merged.lastActiveChatSessionId ?? null,
      lastMoveTargetProjectId: merged.lastMoveTargetProjectId ?? null,
      searchQuery: merged.searchQuery ?? '',
      searchMode: (merged.searchMode as 'fulltext' | 'semantic') ?? 'fulltext',
      documentPositions: serverDocPositions,
    });
  },
  loadProjectPrefs: (userId, projectId) => {
    // Dedup: an in-flight load for this project already covers the hydration.
    if (_projectPrefsPromises.has(projectId)) return;
    set({ projectPrefsLoaded: false });
    const promise = apiClient.get(`/preferences/${projectId}`)
      .then((serverPrefs: Partial<PersistedUIState>) => {
        if (!_appBridge) return;
        const appCtx = _appBridge.getAppContext();
        // INVARIANT: discard only when a DIFFERENT project is already active. A still-null
        // currentProject is the cold-open race (DocumentPage starts this load before the
        // project fetch resolves) — its prefs are for THIS project and must apply.  Why: discard incoming prefs only if a DIFFERENT project is already active; a null currentProject is the cold-open race (DocumentPage started before the project fetch) — its prefs are for this project.
        // Why: gating on `!== projectId` dropped the prefs in that window, so the persisted
        // reference never restored on open.
        const cp = appCtx.currentProject?.project_id;
        if (cp && cp !== projectId) return;
        if (!serverPrefs || Object.keys(serverPrefs).length === 0) return;

        const merged = { ...UI_DEFAULTS, ...serverPrefs };
        lastSavedBlobs.set(`${userId}:${projectId}`, merged);
        get().applyProjectPrefs(merged);
        // No deferred ref restore here: the URL-driven open (DocumentPage) awaits
        // awaitProjectPrefs and commits the reference atomically with the document.
      })
      .catch(() => _appBridge?.showToast('Failed to load settings', 'error'))
      .finally(() => {
        if (_projectPrefsPromises.get(projectId) === promise) {
          _projectPrefsPromises.delete(projectId);
        }
        // Flip the hydration flag unless a DIFFERENT project is already active — a
        // late-resolving stale load must not un-gate a newer project's panel, but a
        // still-null project (cold open) is this project's load and must un-gate.
        const cp = _appBridge?.getAppContext().currentProject?.project_id;
        if (!cp || cp === projectId) {
          set({ projectPrefsLoaded: true });
        }
      });
    _projectPrefsPromises.set(projectId, promise);
  },
  awaitProjectPrefs: (projectId) => {
    return _projectPrefsPromises.get(projectId) ?? Promise.resolve();
  },
  resetForProjectSwitch: (isProjectSwitch) => {
    set({
      projectPrefsLoaded: false,
      ...(isProjectSwitch ? {
        sidebarTab: UI_DEFAULTS.sidebarTab,
        sidebarOpen: UI_DEFAULTS.sidebarOpen,
        collapsedDocIds: UI_DEFAULTS.collapsedDocIds ?? [],
      } : {}),
      documents: {},
      searchTabDocId: null,
      // Drop the prior project's active chat so a stale id does not resolve
      // against the new project's list during the prefs-hydration window.
      lastActiveChatSessionId: null,
      lastMoveTargetProjectId: null,
      searchQuery: '',
      searchResults: [],
      searchMode: 'fulltext' as const,
      documentPositions: {},
    });
  },
}));

/**
 * React hook returning the UI slice for an explicit document id.
 * For unknown / null docId returns the frozen DEFAULT_DOC_STATE — its stable
 * reference keeps Zustand's Object.is selector check from looping.
 * Orphan (no-active-doc) state lives at documents[ORPHAN_KEY].
 */
export function useDocState(docId: string | null | undefined): DocumentUIState {
  return useUIStore(s => s.documents[docId ?? ORPHAN_KEY] ?? DEFAULT_DOC_STATE);
}
