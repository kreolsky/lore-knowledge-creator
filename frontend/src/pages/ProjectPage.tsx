/** Project layout page: sidebar, resizable right panel with tabbed panels, and document outlet. */
// ARCH: Recording lifecycle lives here (not in panel) — survives right panel tab switches.
// ARCH: Reference event listeners always active (not in ReferencesPanel) — panels are conditionally rendered.
// ARCH: Layout chrome is rendered by <ProjectShell> (shared with PublicSharePage). This page
//       owns the authed hooks (collab, recording, refs events) and passes full-mode slots.

import type React from 'react';
import { useCallback, useEffect, useRef } from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import { Sidebar } from '../components/Sidebar';
import { TableOfContents } from '../components/TableOfContents';
import { NotesPanel } from '../components/NotesPanel';
import { ReferencesPanel } from '../components/ReferencesPanel';
import { ChatPanel } from '../components/ChatPanel';
import { HistoryPanel } from '../components/HistoryPanel';
import { SearchPanel } from '../components/SearchPanel';
import { LinksPanel } from '../components/LinksPanel';
import { ProjectSettingsPanel } from '../components/ProjectSettingsPanel';
import { AccessPanel } from '../components/AccessPanel';
import { SnapshotModal } from '../components/SnapshotModal';
import { NoteConnector } from '../components/NoteConnector';
import SelectionToolbar from '../components/editor/SelectionToolbar';
import { FindReplacePanel } from '../components/editor/FindReplacePanel';
import { LinkSuggestionsPopup } from '../components/editor/LinkSuggestionsPopup';
import { EditorLinkPreview } from '../components/editor/EditorLinkPreview';
import { ProjectShell, type ProjectShellHandle, type RightTabEntry, type LeftTabEntry } from '../components/ProjectShell';
import { useAppStore } from '../store/app-store';
import { useUIStore, useDocState, ORPHAN_KEY, type RightTab } from '../store/ui-store';
import { readRefOpenMode, refIsScope } from '../store/ui-store/documents-slice';
import { useNoteStore } from '../store/note-store';
import { useChatStore } from '../store/chat-store';
import { useEvent } from '../hooks/useEvent';
import { useAccessTabState } from '../hooks/useAccessTabState';
import { useInboxSummary } from '../store/inbox-store';
import { ProjectCollabContext } from '../collab/ProjectCollabContext';
import { useTranslation, t as tImperative } from '../i18n';
import { FileText, List, MessageSquare, Paperclip, Archive, MessagesSquare, Search, ArrowDownLeft, Settings, Replace, ShieldCheck } from 'lucide-react';
import { isRightPanelReady } from '../utils/right-panel-ready';
import { useAudioRecorder } from '../hooks/useAudioRecorder';
import { useVoiceInput } from '../hooks/useVoiceInput';
import { useVoiceTranscriptionError } from '../hooks/useVoiceTranscriptionError';
import { deleteActiveSession, hasUnsent, restorePendingRecordings } from '../utils/recording-cache';
import type { Reference, AccessLevel } from '../types';
import { useProjectConnection } from '../hooks/useProjectConnection';
import { useDocumentRoute } from '../hooks/useDocumentRoute';
import { useReferenceEvents } from '../hooks/useReferenceEvents';
import { useRecordingHotkey } from '../hooks/useRecordingHotkey';
import { useSidebarHotkey } from '../hooks/useSidebarHotkey';
import { getEditorView } from '../editor/active-editor';
import { voiceWidgetField, voiceWidgetRemove } from '../components/editor/voice-widget';

/**
 * Allowed right-panel tabs per access role.
 * INVARIANT: Right-panel tab visibility is gated by role. Why: access roles must
 * not reach privileged panels via hidden UI. Allowed sets:
 *   - full:        notes, refs, checkpoint, chat, search, links, settings, find
 *   - commentator: notes, refs, chat, search, links, find (no checkpoint/settings)
 *   - readonly:    refs, search, links, find (no chat/notes)
 * Order defines the fallback when a persisted tab is no longer permitted.
 * INVARIANT: 'find' is allowed for every role so the persisted ephemeral tab is not
 * filtered out as invalid on hydration. Why: the replace row is gated by accessLevel
 * inside FindReplacePanel, not by tab visibility — search is available to all roles.
 * It must NOT be allowedTabs[0] (the fallback), so it is appended last.
 */
function allowedRightTabs(accessLevel: AccessLevel): RightTab[] {
  switch (accessLevel) {
    case 'full':
      // 'access' is full-only AND hidden when a reference is open (render-gate below);
      // it sits beside 'checkpoint' as a document-scoped management surface.
      return ['notes', 'refs', 'checkpoint', 'chat', 'search', 'links', 'settings', 'access', 'find'];
    case 'commentator':
      return ['notes', 'refs', 'chat', 'search', 'links', 'find'];
    case 'readonly':
      return ['refs', 'search', 'links', 'find'];
  }
}

export function ProjectPage() {
  const { projectId, documentId: urlDocId } = useDocumentRoute();
  const location = useLocation();
  const { t } = useTranslation();
  const hasDocument = location.pathname.includes('/docs/');
  const currentReference = useAppStore(s => s.currentReference);

  const shellRef = useRef<ProjectShellHandle>(null);

  const setRightPanelTab = useUIStore(s => s.setRightPanelTab);
  const setFindSeed = useUIStore(s => s.setFindSeed);
  const activeNoteThreadId = useNoteStore(s => s.activeNoteThreadId);
  // Yellow the chat TAB while the panel
  // is in its new-chat/ghost state (activeSessionId === null). Mirrors the
  // activeNoteThreadId tab-tint subscription — same reactive cost, no new perf
  // class. CSS defines only the .active variant so a collapsed/background chat
  // tab stays neutral (avoiding the false "a note is open" signal).
  const isGhostChat = useChatStore(s => s.activeSessionId) === null;

  const currentDocId = useAppStore(s => s.currentDocument?.document_id ?? null);
  const docState = useDocState(currentDocId);
  const rightPanelTab = docState.rightPanelTab;
  // WHY: gate panel content (and tab highlight) on prefs hydration AND on the
  // URL-driven document being committed — BUT only force the loading state when there
  // is nothing valid to fall back on.  Why: gate on prefs+committed-doc, but only force the loading spinner when there's no valid fallback — avoids a flash when a stale-but-valid tab can show.
  // Why: projectPrefsLoaded flips in the prefs load's .finally() — BEFORE the atomic
  // doc commit. On cold load / deep-link / F5, currentDocId is null in that window, so
  // the per-doc slice falls back to DEFAULT_DOC_STATE's tab and the panel would
  // flick default → real saved tab (and mount a panel only to discard it). Keep loading on
  // in that case.
  // In-app navigation (docA -> docB) is different: currentDocId is the PREVIOUS doc
  // (non-null). Keeping the panel mounted there (ready=true) prevents a bare
  // PanelLoading frame + a ChatPanel remount that flashes the previous document's chat
  // ("spinner → stale chat → spinner → new chat"). The inner chatScopeLoading spinner
  // becomes the single loading indicator.
  const projectPrefsLoaded = useUIStore(s => s.projectPrefsLoaded);
  const docPending = !!urlDocId && currentDocId !== urlDocId;
  const rightPanelReady = isRightPanelReady({ projectPrefsLoaded, docPending, currentDocId });
  const accessLevel = useAppStore(s => s.accessLevel);
  const allowedTabs = allowedRightTabs(accessLevel);
  // Ephemeral search overlay (see documents-slice INVARIANT): while alive for the
  // current doc (panel open), search renders + highlights as active instead of the
  // stored tab. allowedRightTabs includes 'search' for every role, so the overlay
  // branch needs no extra role validation.
  const searchOverlay = useUIStore(s => s.searchTabDocId === (currentDocId ?? ORPHAN_KEY));
  // INVARIANT: validate persisted rightPanelTab against the role-allowed set.
  // If the saved tab is no longer permitted, fall back to the first allowed tab.  Why: a persisted tab can become disallowed by a role change; validating against the allowed set prevents rendering a tab the user can't access.
  const effectiveTab = rightPanelReady
    ? (searchOverlay
      ? 'search'
      : (rightPanelTab && allowedTabs.includes(rightPanelTab) ? rightPanelTab : allowedTabs[0]))
    : null;

  // Persist the fallback when the saved tab is no longer permitted for this role
  // (e.g. role changed, or localStorage carried a full-only tab into a lower role).
  useEffect(() => {
    if (rightPanelReady && rightPanelTab && !allowedTabs.includes(rightPanelTab)) {
      setRightPanelTab(currentDocId, allowedTabs[0]);
    }
  }, [rightPanelReady, rightPanelTab, allowedTabs, currentDocId, setRightPanelTab]);

  // see SYSTEM: selection-region-agent — the editor's Bot icon emits 'start-agent-chat'
  // when the user picks "Work with selection". Wired here (always mounted in a
  // project) rather than in ChatPanel (unmounts on tab switch) so the event is
  // caught even when the chat tab is closed; startAgentChat reveals the panel.
  useEvent('start-agent-chat', payload => {
    void useChatStore.getState().startAgentChat(payload);
  });

  const { projectCollabConn } = useProjectConnection({ projectId });
  useReferenceEvents();
  useRecordingHotkey();
  useSidebarHotkey();

  const addReference = useAppStore(s => s.addReference);
  const updateReference = useAppStore(s => s.updateReference);
  const replaceReference = useAppStore(s => s.replaceReference);
  const { handleVoiceRecording } = useVoiceInput();
  useVoiceTranscriptionError();

  // Boot restore: takes the previous session left unsent (tab closed
  // mid-upload, crash mid-recording) are re-sent through the async path, deduped
  // per project by an in-flight promise (StrictMode double-invoke).
  useEffect(() => {
    if (!projectId) return;
    void restorePendingRecordings(projectId);
  }, [projectId]);

  // Best-effort close guard while a take is rolling or unsent recordings sit in
  // the cache — hasUnsent() is synchronous because this handler cannot await.
  // The cache, not this guard, is the guarantee (mobile browsers skip beforeunload).
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (useAppStore.getState().recording || hasUnsent()) {
        e.preventDefault();
        e.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, []);

  /**
   * Called when MediaRecorder finishes (recorder.onstop).
   * Routes to the appropriate flow:
   *   1. handleVoiceRecording — if a Cmd+D voice widget is active, does sync
   *      upload-and-transcribe and inserts text at the widget position.
   *   2. Async fallback — if no widget (header button recording), creates a
   *      reference and uploads for async transcription.
   */
  const handleRecordingComplete = useCallback(async (file: File, ctx: { projectId: string; documentId: string; sessionId: string }) => {
    const handled = await handleVoiceRecording(file, ctx);
    if (handled) return;

    // Defensive cleanup: if handleVoiceRecording returned false (didn't find the
    // voice widget — e.g. editor view was temporarily unavailable during
    // recorder.onstop), the async flow below will create a reference but the CM6
    // voice widget would remain stuck in "recognizing" state. Remove it here.
    try {
      const view = getEditorView();
      const ws = view?.state.field(voiceWidgetField, false);
      if (ws && view) view.dispatch({ effects: voiceWidgetRemove.of(undefined) });
    } catch { /* no widget — nothing to clean up */ }

    const tempId = `temp_${Date.now()}_${file.name}`;
    const tempRef: Reference = {
      reference_id: tempId,
      project_id: ctx.projectId,
      document_id: ctx.documentId,
      title: file.name,
      media_type: 'audio',
      processing_status: 'uploading',
      source_url: null, content: '', file_path: null, file_meta: null,
      updated_at: new Date().toISOString(), created_at: new Date().toISOString(),
    };
    addReference(tempRef);

    const formData = new FormData();
    formData.append('file', file);
    formData.append('project_id', ctx.projectId);
    formData.append('document_id', ctx.documentId);
    formData.append('title', file.name);
    // Replay guard: THIS take's cache session id (carried by ctx since record
    // start — never a global read) makes a lost-2xx re-send replay the SAME
    // reference instead of duplicating it (SYSTEM: recording-cache).
    formData.append('idempotency_key', ctx.sessionId);

    try {
      const { apiClient } = await import('../api/client');
      const ref = await apiClient.upload('/references/upload', formData);
      replaceReference(tempId, ref);
      // 2xx ack → drop the cached copy (see the delete-only-after-2xx rule in
      // SYSTEM: recording-cache).
      void deleteActiveSession();
    } catch (err) {
      console.error('Failed to upload recording', err);
      // Error-keep (mirrors useReferenceUpload): the take stays in the cache and
      // is re-sent when the project is next opened. The temp ref must not hang
      // in `uploading` forever — no silent degradation.
      updateReference(tempId, { processing_status: 'error' });
      useAppStore.getState().showToast(tImperative('recordingKeptForRetry'), 'error');
    }
  }, [addReference, replaceReference, updateReference, handleVoiceRecording]);

  useAudioRecorder(handleRecordingComplete);

  useEvent('ws:project_updated', useCallback(({ updates }) => {
    const project = useAppStore.getState().currentProject;
    if (!project) return;
    const merged = { ...project, ...updates } as typeof project;
    const changed = (Object.keys(updates) as (keyof typeof project)[]).some(
      k => project[k] !== merged[k],
    );
    if (changed) useAppStore.getState().setCurrentProject(merged);
  }, []));

  useEvent('open-notes', useCallback((payload: { threadId?: string }) => {
    const docId = useAppStore.getState().currentDocument?.document_id ?? null;
    if (payload?.threadId) {
      useNoteStore.getState().setActiveNoteThreadId(payload.threadId, docId ?? undefined);
    }
    shellRef.current?.openRightPanel('notes');
  }, []));

  useEvent('open-find', useCallback(({ selection }: { selection: string }) => {
    // ARCH: seed the search field with the editor selection before the tab switch
    // mounts FindReplacePanel (which reads this once on mount). The live open-find
    // subscription inside FindReplacePanel handles the repeat-Cmd+F case.
    setFindSeed(selection);
    shellRef.current?.openRightPanel('find');
  }, [setFindSeed]));

  useEvent('open-sidebar-docs', useCallback(() => {
    shellRef.current?.openSidebar('docs');
  }, []));

  useEvent('open-right-panel', useCallback(() => {
    shellRef.current?.openRightPanel();
  }, []));

  // Panel quick preview: the doc is the scope, so the doc-scoped right tabs
  // (Checkpoint, Access) exist while a reference is merely previewed. The
  // reveal effect below keeps reading currentReference RAW — it is the mechanism
  // that makes the preview visible in the Refs tab.
  const refOpenMode = readRefOpenMode(docState, useUIStore(s => s.compactLayout));
  const isReference = !!(currentReference && refIsScope(refOpenMode));
  const setSnapshotPreview = useAppStore(s => s.setSnapshotPreview);

  // Access-tab tint: strongest active external access on the current doc.
  const currentProject = useAppStore(s => s.currentProject);
  const accessTint = useAccessTabState(
    currentDocId, currentProject?.project_id ?? null, !!currentProject?.is_public,
  );

  // see SYSTEM: inbox — the caller's unread pool for this project (loaded once,
  // refetched on the owner-filtered ws:inbox_changed frame). Drives the
  // notes/refs tab tints here; the tree reads the same store slice.
  const inboxCounts = useInboxSummary(currentProject?.project_id, currentDocId);

  // Side effect of a right-tab click: exit snapshot preview unless the user is
  // actively opening the checkpoint tab. (Snapshot preview is the only authed-mode
  // state tied to right-tab clicks; lives here, not in the Shell.)
  const handleRightTabClick = useCallback((tab: RightTab) => {
    if (tab !== 'checkpoint') setSnapshotPreview(null);
  }, [setSnapshotPreview]);

  // In 'panel' mode the open reference renders inside the Refs tab; the tab is
  // revealed by the navigate-to-reference handler (see INVARIANT in
  // hooks/useEditorEvents.ts), never by a watcher on currentReference here.

  const handlePanelDragEnter = useCallback((e: React.DragEvent) => {
    if (!e.dataTransfer.types.includes('Files')) return;
    if (effectiveTab === 'chat') {
      shellRef.current?.openRightPanel('chat');
      return;
    }
    // WHY: with an open note thread, NotesPanel is mounted and its useImageDropHandlers
    // zone (enabled: isThreadView) must capture the drop — identical policy to AI chat.
    // Mirrors the chat exemption above so the refs fallback never steals a note image drop.
    if (effectiveTab === 'notes' && activeNoteThreadId) {
      shellRef.current?.openRightPanel('notes');
      return;
    }
    if (effectiveTab !== 'refs') shellRef.current?.openRightPanel('refs');
    else shellRef.current?.openRightPanel();
  }, [effectiveTab, activeNoteThreadId]);

  // ── Tab entries (full-mode: all role-permitted tabs) ─────────────────────
  const leftTabs: LeftTabEntry[] = [
    { tab: 'docs', icon: <FileText size={15} />, title: t('tabDocuments'), renderPanel: () => <Sidebar /> },
    { tab: 'toc', icon: <List size={15} />, title: t('tabTableOfContents'), renderPanel: () => <TableOfContents /> },
  ];

  const rightTabs: RightTabEntry[] = [];
  if (accessLevel !== 'readonly') {
    rightTabs.push({
      tab: 'chat',
      icon: <MessagesSquare size={15} />,
      title: t('tabChat'),
      renderPanel: () => <ChatPanel />,
      className: isGhostChat ? 'chat-tab-new' : undefined,
    });
  }
  rightTabs.push({
    tab: 'refs',
    icon: <Paperclip size={15} />,
    title: t('tabReferences'),
    renderPanel: () => <ReferencesPanel />,
    className: inboxCounts.refs > 0 ? 'refs-tab-unread' : undefined,
  });
  if (accessLevel !== 'readonly') {
    rightTabs.push({
      tab: 'notes',
      icon: <MessageSquare size={15} />,
      title: t('tabNotes'),
      renderPanel: () => <NotesPanel />,
      // One yellow, one meaning (plan decision): the thread-open tint and the
      // inbox-unread tint compose — both are "attention on notes".
      className: [
        activeNoteThreadId ? 'notes-tab-thread' : null,
        inboxCounts.notes > 0 ? 'notes-tab-unread' : null,
      ].filter(Boolean).join(' ') || undefined,
    });
  }
  if (!isReference && accessLevel === 'full') {
    rightTabs.push({ tab: 'checkpoint', icon: <Archive size={15} />, title: t('tabHistory'), renderPanel: () => <HistoryPanel /> });
  }
  rightTabs.push({ tab: 'links', icon: <ArrowDownLeft size={18} />, title: t('tabLinks'), renderPanel: () => <LinksPanel /> });
  if (!isReference && accessLevel === 'full') {
    // Access tab — full-only, hidden when a reference is open. Tinted by the
    // strongest active external access (red/yellow/blue) via useAccessTabState.
    rightTabs.push({
      tab: 'access',
      icon: <ShieldCheck size={15} />,
      title: t('tabAccess'),
      renderPanel: () => <AccessPanel />,
      className: `access-tab--${accessTint}`,
    });
  }
  // Ephemeral find tab: rendered only while it is the active tab — switching
  // to any other tab overwrites rightPanelTab, so the button vanishes.
  if (effectiveTab === 'find') {
    rightTabs.push({ tab: 'find', icon: <Replace size={15} />, title: t('tabFind'), renderPanel: () => <FindReplacePanel /> });
  }
  rightTabs.push({ tab: 'search', icon: <Search size={15} />, title: t('tabSearch'), renderPanel: () => <SearchPanel />, aside: true });
  if (accessLevel === 'full') {
    rightTabs.push({ tab: 'settings', icon: <Settings size={15} />, title: t('tabSettings'), renderPanel: () => <ProjectSettingsPanel />, aside: true });
  }

  return (
    <ProjectCollabContext.Provider value={projectCollabConn}>
      <ProjectShell
        ref={shellRef}
        leftTabs={leftTabs}
        rightTabs={rightTabs}
        renderCenter={() => hasDocument ? <Outlet /> : (
          <div className="h-full flex items-center justify-center text-text-dim bg-bg">
            {t('selectOrCreateDocument')}
          </div>
        )}
        userControlsVariant="full"
        rightPanelDocId={currentDocId}
        rightPanelReady={rightPanelReady}
        effectiveRightTab={effectiveTab}
        onPanelDragEnter={handlePanelDragEnter}
        onRightTabClick={handleRightTabClick}
        overlays={
          <>
            <SnapshotModal />
            <NoteConnector />
            {/* ARCH: editor floating UI rendered ONCE at project level (not per Editor) so the
                two split-view columns share one toolbar/popup/preview. Each reads the focused
                EditorView via editor/active-editor, so they target whichever column has focus. */}
            <SelectionToolbar />
            <LinkSuggestionsPopup />
            <EditorLinkPreview />
          </>
        }
      />
    </ProjectCollabContext.Provider>
  );
}
