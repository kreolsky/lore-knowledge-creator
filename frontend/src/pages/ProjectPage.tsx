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
import { DockedFormatToolbar, useDockedToolbarOn } from '../components/editor/DockedFormatToolbar';
import ImageToolbar from '../components/editor/ImageToolbar';
import { FindReplacePanel } from '../components/editor/FindReplacePanel';
import { LinkSuggestionsPopup } from '../components/editor/LinkSuggestionsPopup';
import { EditorLinkPreview } from '../components/editor/EditorLinkPreview';
import { ImagePreviewHost } from '../components/editor/ImagePreviewHost';
import { ProjectShell, type ProjectShellHandle, type RightTabEntry, type LeftTabEntry, type ShellTab } from '../components/ProjectShell';
import { ChatModeList } from '../components/chat/ChatModeList';
import { ChatModeDocHeader } from '../components/chat/ChatModeDocHeader';
import { useChatDocFollow } from '../hooks/useChatDocFollow';
import { useAppStore } from '../store/app-store';
import { selectOpenPageLocked, PAGE_BOUND_TABS } from '../utils/page-lock';
import { useUIStore, useDocState, selectChatMode, ORPHAN_KEY, type RightTab } from '../store/ui-store';
import { isRememberedTab, readRefOpenMode, refIsScope } from '../store/ui-store/documents-slice';
import { searchModeOnOpen } from '../store/ui-store/search-slice';
import { useNoteStore } from '../store/note-store';
import { useChatStore } from '../store/chat-store';
import { clarifyBlock } from '../components/chat/clarify-format';
import { useEvent } from '../hooks/useEvent';
import { useAccessTabState } from '../hooks/useAccessTabState';
import { useInboxSummary } from '../store/inbox-store';
import { ProjectCollabContext } from '../collab/ProjectCollabContext';
import { useTranslation, t as tImperative } from '../i18n';
import { FileText, List, StickyNote, Paperclip, Archive, MessagesSquare, MessageSquareText, Search, ArrowDownLeft, Settings, Replace, ShieldCheck } from 'lucide-react';
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
import { useTextWidthHotkey } from '../hooks/useTextWidthHotkey';
import { getEditorView } from '../editor/active-editor';
import { voiceWidgetField, voiceWidgetRemove } from '../components/editor/voice-widget';
import { uploadReferenceFile } from '../api/reference-upload';

/**
 * Allowed right-panel tabs per access role × account tier.
 * INVARIANT: Right-panel tab visibility is gated by project access × account
 * tier (see SYSTEM: capability-tiers). Why: access roles must not reach privileged
 * panels via hidden UI, and a non-ai account must not reach the chat panel —
 * the server 403 (tier_required:ai) on session create/turn is the guarantee,
 * the tab list only removes the trigger. Allowed sets:
 *   - full:        notes, refs, checkpoint, chat, search, links, settings, access, find
 *   - commentator: notes, refs, chat, search, links, find (no checkpoint/settings)
 *   - readonly:    refs, search, links, find (no chat/notes)
 * 'chat' drops from every set when the account's tier flag denies ai
 * (can_ai === false). Order defines the fallback when a persisted tab is no
 * longer permitted — the first REMEMBERED entry (chat/refs/notes) wins; a technical
 * tab can never be the stored tab (documents-slice INVARIANT).
 * INVARIANT: 'find' is allowed for every role so the find overlay passes the same
 * allowedTabs validation as every other tab. Why: the replace row is gated by accessLevel
 * inside FindReplacePanel, not by tab visibility — search is available to all roles.
 * It must NOT be allowedTabs[0] (the fallback), so it is appended last.
 */
export function allowedRightTabs(accessLevel: AccessLevel, canAi = true): RightTab[] {
  const withTier = (tabs: RightTab[]): RightTab[] =>
    canAi ? tabs : tabs.filter(tab => tab !== 'chat');
  switch (accessLevel) {
    case 'full':
      // 'access' is full-only AND hidden when a reference is open (render-gate below);
      // it sits beside 'checkpoint' as a document-scoped management surface.
      return withTier(['notes', 'refs', 'checkpoint', 'chat', 'search', 'links', 'settings', 'access', 'find']);
    case 'commentator':
      return withTier(['notes', 'refs', 'chat', 'search', 'links', 'find']);
    case 'readonly':
      return ['refs', 'search', 'links', 'find'];
  }
}

export function ProjectPage() {
  const { projectId, documentId: urlDocId } = useDocumentRoute();
  const location = useLocation();
  const { t } = useTranslation();
  const hasDocument = location.pathname.includes('/docs/');
  const dockedToolbarOn = useDockedToolbarOn();
  const currentReference = useAppStore(s => s.currentReference);

  const shellRef = useRef<ProjectShellHandle>(null);
  const chatModeRef = useRef(false);

  const setRightPanelTab = useUIStore(s => s.setRightPanelTab);
  const setFindSeed = useUIStore(s => s.setFindSeed);
  const activeNoteThreadId = useNoteStore(s => s.activeNoteThreadId);
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
  // see SYSTEM: capability-tiers — can_ai from /me drops the chat tab (see
  // allowedRightTabs); undefined reads as allowed (an old payload predating
  // the flags must not hide chat).
  const canAi = useAppStore(s => s.currentUser?.can_ai) !== false;
  // INVARIANT(security): a PIN-locked page not yet opened by PIN offers only the
  // project-level tabs (chat, search, settings); every page-bound tab drops and a stored
  // one falls back like a role-forbidden tab. Why: operator request — everything bound
  // to the page (notes, refs, history, links, access, find) stays behind the PIN like its text.
  const pageLocked = useAppStore(selectOpenPageLocked);
  const allowedTabs = allowedRightTabs(accessLevel, canAi)
    .filter(tab => !(pageLocked && (PAGE_BOUND_TABS as readonly string[]).includes(tab)));
  // Ephemeral technical-tab overlay (see documents-slice INVARIANT): while alive for the
  // current doc, that tab renders + highlights as active instead of the stored tab.
  // The rendered entries below are built from allowedTabs (role × tier, PIN lock), and
  // effectiveTab resolves the overlay against them — the same validation as the stored tab.
  const overlayTab = useUIStore(s => {
    const o = s.tabOverlay;
    return o && o.docId === (currentDocId ?? ORPHAN_KEY) ? o.tab : null;
  });
  // The fallback for a stored tab this role no longer permits: the first allowed REMEMBERED
  // tab, null when role × PIN lock leave none (a readonly role on a locked page keeps only
  // search). Why: the fallback is persisted (effect below) and only a remembered tab is
  // storable — a technical one would be taken as an overlay and the effect would re-fire.
  const fallbackTab = allowedTabs.find(isRememberedTab) ?? null;
  // INVARIANT: validate persisted rightPanelTab against the role-allowed set. Why: a persisted
  // tab can become disallowed by a role change; validating against the allowed set prevents
  // rendering a tab the user can't access — the saved tab falls back to the fallback tab (or
  // the first allowed one when no remembered tab is permitted).
  const storedTab = rightPanelTab && allowedTabs.includes(rightPanelTab) ? rightPanelTab : (fallbackTab ?? allowedTabs[0]);

  // Persist the fallback when the saved tab is no longer permitted for this role
  // (e.g. role changed, or localStorage carried a full-only tab into a lower role).
  useEffect(() => {
    if (rightPanelReady && fallbackTab && rightPanelTab && !allowedTabs.includes(rightPanelTab)) {
      setRightPanelTab(currentDocId, fallbackTab);
    }
  }, [rightPanelReady, rightPanelTab, allowedTabs, currentDocId, setRightPanelTab, fallbackTab]);

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

    // Replay guard: THIS take's cache session id (carried by ctx since record
    // start — never a global read) makes a lost-2xx re-send replay the SAME
    // reference instead of duplicating it (SYSTEM: recording-cache).

    try {
      const ref = await uploadReferenceFile(ctx.projectId, ctx.documentId, file, { idempotencyKey: ctx.sessionId });
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
      // see SYSTEM: capability-tiers — a downgrade mid-take is refused 403
      // tier_required:voice; the take is kept all the same, the toast names why.
      const { tierRefusal } = await import('../utils/server-refusal-detail');
      useAppStore.getState().showToast(
        tImperative(tierRefusal(err) !== null ? 'tierVoiceRequired' : 'recordingKeptForRetry'),
        'error',
      );
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

  // WHY here, not in ChatInput: ProjectPage is always mounted, the chat tab is not — a
  // quote sent from the editor while the panel is closed would otherwise be lost.
  useEvent('chat-quote-insert', useCallback(({ quote, source, question }: { quote: string; source?: string; question?: string }) => {
    const block = clarifyBlock(quote, source, question);
    // Append to the store draft (never overwrite — the user may have typed more).
    const { draft, setDraft } = useChatStore.getState();
    setDraft(draft ? `${draft}\n\n${block}` : block);
    useChatStore.setState({ pendingInputFocus: true });
    shellRef.current?.openRightPanel('chat');
  }, []));

  useEvent('open-find', useCallback(({ selection }: { selection: string }) => {
    // ARCH: seed the search field with the editor selection before the tab switch
    // mounts FindReplacePanel (which reads this once on mount). The live open-find
    // subscription inside FindReplacePanel handles the repeat-Cmd+F case.
    // WHY: chat mode's right panel has no find tab — Cmd+F there would open a hidden overlay.
    if (chatModeRef.current) return;
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
    currentDocId, currentProject?.project_id ?? null,
    (currentProject?.audiences?.length ?? 0) > 0,
  );

  // see SYSTEM: inbox — the caller's unread pool for this project (loaded once,
  // refetched on the owner-filtered ws:inbox_changed frame). Drives the
  // notes/refs tab tints here; the tree reads the same store slice.
  const inboxCounts = useInboxSummary(currentProject?.project_id, currentDocId);

  // Side effects of a right-tab click: exit snapshot preview unless the user is
  // actively opening the checkpoint tab, and pick the Search tab's starting mode
  // from the tab it is opened FROM (the one visible before the click; null while
  // the panel was closed). The click's tab write re-renders after this handler,
  // and SearchPanel (sharing the right panel with chat) mounts after that,
  // reading the mode set here.
  const handleRightTabClick = useCallback((tab: ShellTab, fromTab: ShellTab | null) => {
    if (tab !== 'checkpoint') setSnapshotPreview(null);
    if (tab === 'search') {
      const ui = useUIStore.getState();
      const next = searchModeOnOpen(fromTab === 'doc' ? null : fromTab, ui.searchMode, ui.searchDocMode);
      if (next) ui.setSearchMode(next);
    }
  }, [setSnapshotPreview]);

  // In 'panel' mode the open reference renders inside the Refs tab; the tab is
  // revealed by the navigate-to-reference handler (see INVARIANT in
  // hooks/useEditorEvents.ts), never by a watcher on currentReference here.

  // ── Tab entries (full-mode: all role-permitted tabs) ─────────────────────
  const leftTabs: LeftTabEntry[] = [
    { tab: 'docs', icon: <FileText size={15} />, title: t('tabDocuments'), renderPanel: () => <Sidebar /> },
    { tab: 'toc', icon: <List size={15} />, title: t('tabTableOfContents'), renderPanel: () => <TableOfContents /> },
  ];
  // Chat mode (the chats left tab): offered on a desktop layout to whoever has the chat tab.
  // Ruling: a phone keeps docs mode only (see selectChatMode).
  const compactLayout = useUIStore(s => s.compactLayout);
  const offersChatMode = !compactLayout && canAi && accessLevel !== 'readonly';
  if (offersChatMode) {
    leftTabs.push({ tab: 'chats', icon: <MessageSquareText size={15} />, title: t('tabChats'), renderPanel: () => <ChatModeList /> });
  }
  const chatMode = useUIStore(selectChatMode) && offersChatMode;
  const chatModeRightTab = useUIStore(s => s.chatModeRightTab);
  chatModeRef.current = chatMode;
  // WHY: chat mode and document mode each remember their own text-column width.
  useTextWidthHotkey(projectId && (chatMode ? `${projectId}.chat` : projectId));
  useChatDocFollow(chatMode);

  const rightTabs: RightTabEntry[] = [];
  if (accessLevel !== 'readonly') {
    rightTabs.push({
      tab: 'chat',
      icon: <MessagesSquare size={15} />,
      title: t('tabChat'),
      renderPanel: () => <ChatPanel />,
    });
  }
  if (allowedTabs.includes('refs')) {
    rightTabs.push({
      tab: 'refs',
      icon: <Paperclip size={15} />,
      title: t('tabReferences'),
      renderPanel: () => <ReferencesPanel />,
      className: inboxCounts.refs > 0 ? 'refs-tab-unread' : undefined,
    });
  }
  if (allowedTabs.includes('notes')) {
    rightTabs.push({
      tab: 'notes',
      icon: <StickyNote size={15} />,
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
  if (!isReference && allowedTabs.includes('checkpoint')) {
    rightTabs.push({ tab: 'checkpoint', icon: <Archive size={15} />, title: t('tabHistory'), renderPanel: () => <HistoryPanel /> });
  }
  if (allowedTabs.includes('links')) {
    rightTabs.push({ tab: 'links', icon: <ArrowDownLeft size={18} />, title: t('tabLinks'), renderPanel: () => <LinksPanel /> });
  }
  if (!isReference && allowedTabs.includes('access')) {
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
  // to any other tab drops the overlay, so the button vanishes.
  if (overlayTab === 'find') {
    rightTabs.push({ tab: 'find', icon: <Replace size={15} />, title: t('tabFind'), renderPanel: () => <FindReplacePanel /> });
  }
  rightTabs.push({ tab: 'search', icon: <Search size={15} />, title: t('tabSearch'), renderPanel: () => <SearchPanel />, aside: true });
  if (accessLevel === 'full') {
    rightTabs.push({ tab: 'settings', icon: <Settings size={15} />, title: t('tabSettings'), renderPanel: () => <ProjectSettingsPanel />, aside: true });
  }

  // The rendered tab: the live technical overlay when its entry is rendered, else the
  // stored tab. Why resolve against the entries: a reference open in the main area hides
  // checkpoint/access and a PIN-locked page hides the page-bound tabs — an overlay whose
  // entry vanished must fall back to the stored tab, not mount an empty right panel.
  const effectiveTab: RightTab | null = rightPanelReady
    ? (overlayTab && rightTabs.some(entry => entry.tab === overlayTab) ? overlayTab : storedTab)
    : null;

  // Chat mode's right panel: the document (kept mounted — DocumentPage commits the URL
  // document whatever tab is shown), search, settings. Notes, materials, history and links
  // are not in chat mode (operator ruling).
  const renderDocumentBody = () => hasDocument ? (
    // WHY above the Outlet: one docked bar spans both split columns and serves
    // whichever editor has focus, like the floating overlays below.
    <div className="flex-1 flex flex-col min-h-0 min-w-0 overflow-hidden">
      {dockedToolbarOn && <DockedFormatToolbar />}
      <Outlet />
    </div>
  ) : (
    <div className="h-full flex items-center justify-center px-6 text-center text-text-dim bg-bg">
      {chatMode ? t('chatModeNoDocument') : t('selectOrCreateDocument')}
    </div>
  );
  const renderDocument = () => chatMode ? (
    <div className="chat-mode-doc flex-1 flex flex-col min-h-0 min-w-0 overflow-hidden">
      <ChatModeDocHeader />
      {renderDocumentBody()}
    </div>
  ) : renderDocumentBody();
  const chatModeTabs: RightTabEntry[] = rightTabs
    .filter(entry => entry.tab === 'search' || entry.tab === 'settings');
  chatModeTabs.unshift({ tab: 'doc', icon: <FileText size={15} />, title: t('tabDocument'), renderPanel: renderDocument, keepMounted: true });
  const chatModeTab: ShellTab = chatModeTabs.some(entry => entry.tab === chatModeRightTab) ? chatModeRightTab : 'doc';

  const handlePanelDragEnter = useCallback((e: React.DragEvent) => {
    if (!e.dataTransfer.types.includes('Files')) return;
    // WHY: chat mode's right panel has no Refs/chat tab to reveal for a drop.
    if (chatMode) return;
    // WHY: a locked page has no Refs tab to reveal (page-bound tabs filter above).
    if (pageLocked) return;
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
  }, [chatMode, effectiveTab, activeNoteThreadId, pageLocked]);

  return (
    <ProjectCollabContext.Provider value={projectCollabConn}>
      <ProjectShell
        ref={shellRef}
        leftTabs={leftTabs}
        rightTabs={chatMode ? chatModeTabs : rightTabs}
        renderCenter={chatMode ? () => <ChatPanel /> : renderDocument}
        userControlsVariant="full"
        rightPanelDocId={currentDocId}
        rightPanelReady={chatMode || rightPanelReady}
        effectiveRightTab={chatMode ? chatModeTab : effectiveTab}
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
            <ImageToolbar />
            <LinkSuggestionsPopup />
            <EditorLinkPreview />
            <ImagePreviewHost />
          </>
        }
      />
    </ProjectCollabContext.Provider>
  );
}
