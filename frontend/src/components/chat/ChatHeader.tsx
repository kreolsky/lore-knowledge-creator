/** Chat header — static title (double-click to rename) + new chat / goto-parent / export / delete. Chat-store slices: sessions, activeSessionId, modelsLoaded, createSession, setActiveSession, deleteSession, updateSession, loadModels. App-store slices: currentProject, currentDocument, currentReference. */

import { useState, useCallback, useEffect } from 'react';
import { Plus, Trash2, Download, FileText, Network, Clock, FolderUp, Search } from 'lucide-react';
import { Button, IconButton, FieldInput, Popover } from '../ui';
import { ParentPickerPopup } from '../ParentPickerPopup';
import { useChatStore, selectBranchPath } from '../../store/chat-store';
import { useAppStore } from '../../store/app-store';
import { useUIStore } from '../../store/ui-store';
import { readRefOpenMode, refIsScope } from '../../store/ui-store/documents-slice';
import { navigateToDocKeepingChat } from '../../chat/navigate';
import { emit } from '../../events';
import { useTranslation } from '../../i18n';
import { useArmedAction } from '../../hooks/useArmedAction';
import { CHAT_LIST_FILTER_MIN } from './useSortedSessions';

export function ChatHeader() {
  const sessions = useChatStore(s => s.sessions);
  const activeSessionId = useChatStore(s => s.activeSessionId);
  const modelsLoaded = useChatStore(s => s.modelsLoaded);

  const deleteSession = useChatStore(s => s.deleteSession);
  const updateSession = useChatStore(s => s.updateSession);
  const loadModels = useChatStore(s => s.loadModels);
  const startGhostChat = useChatStore(s => s.startGhostChat);
  const listFilter = useChatStore(s => s.listFilter);
  const setListFilter = useChatStore(s => s.setListFilter);

  const activeSession = sessions.find(s => s.session_id === activeSessionId);
  const documents = useAppStore(s => s.documents);
  const currentDocumentId = useAppStore(s => s.currentDocument?.document_id);
  const rawReferenceId = useAppStore(s => s.currentReference?.reference_id);
  // Same projection as ChatRow: in panel quick preview the chat's scope is the
  // document, so a ref-session is not "current" and goto-parent shows.
  const refOpenMode = useUIStore(s => readRefOpenMode(s.documents[currentDocumentId ?? ''], s.compactLayout));
  const currentReferenceId = refIsScope(refOpenMode) ? rawReferenceId : null;
  // per-document proximity-sort preference (reactive read). The toggle in the
  // ghost plaque flips this; useSortedSessions consumes it to switch sort modes.
  const chatSortByProximity = useUIStore(s => s.getChatSortByProximity(currentDocumentId));
  const setChatSortByProximity = useUIStore(s => s.setChatSortByProximity);

  const { t } = useTranslation();
  const deleteAction = useArmedAction();
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  // Change-parent picker anchor (plan chat-reparent-from-header). Null = closed;
  // the rect is captured from the slot's IconButton at click time — never from
  // the picker itself — mirroring the ReferencesPanel call site.
  const [parentPickerRect, setParentPickerRect] = useState<DOMRect | null>(null);

  useEffect(() => {
    if (!modelsLoaded) loadModels();
  }, [modelsLoaded, loadModels]);

  // The list search is stateless: opening a chat or leaving the panel (tab
  // switch unmounts ChatHeader) drops it. See ChatState.listFilter.
  useEffect(() => {
    if (activeSessionId) setListFilter(null);
  }, [activeSessionId, setListFilter]);
  useEffect(() => () => setListFilter(null), [setListFilter]);

  const handleNewChat = useCallback(() => {
    // ARCH: "Add chat" opens a fresh client-only
    // ghost (no DB row). Every entry point is now a ghost; materialization happens
    // on the first sent message. No parent_session_id inheritance — a ghost has no
    // parent. Inheritance is split: the backend's project-latest walk covers
    // model + system_prompt_id ONLY; agent_auto + model are re-inherited
    // client-side by initGhostFromScope (startGhostChat) so the ghost visibly
    // carries the last active chat's values.
    // A no-op when already on a ghost (already blank).
    if (!activeSessionId) return;
    startGhostChat();
  }, [activeSessionId, startGhostChat]);

  const handleExportJson = useCallback(() => {
    const messages = selectBranchPath(useChatStore.getState());
    const data = {
      session: activeSession,
      messages: messages.map(m => ({
        role: m.role,
        content: m.content,
        images: m.images,
        created_at: m.created_at,
      })),
      exported_at: new Date().toISOString(),
    };
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `chat-${activeSession?.title || 'untitled'}-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }, [activeSession]);

  const handleExportMarkdown = useCallback(() => {
    const messages = selectBranchPath(useChatStore.getState());
    const documents = useAppStore.getState().documents;
    const model = activeSession?.model || '';
    // ARCH: the persona label now derives from the
    // session's SELECTED persona doc (system_prompt_id), not the retired project
    // prompts-folder. Personas ≡ system prompts.
    const promptsDocTitle = activeSession?.system_prompt_id
      ? documents.find(d => d.document_id === activeSession.system_prompt_id)?.title ?? ''
      : '';

    const parts: string[] = [];
    if (activeSession?.title) {
      parts.push(`# ${activeSession.title}`, '');
    }

    for (const m of messages) {
      const date = m.created_at
        ? new Date(m.created_at).toLocaleString(undefined, { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
        : '';
      if (m.role === 'user') {
        parts.push(`## User`);
      } else {
        const aiLabel = model || promptsDocTitle
          ? `## AI${model ? ` (${model}${promptsDocTitle ? ` | ${promptsDocTitle}` : ''})` : promptsDocTitle ? ` (${promptsDocTitle})` : ''}`
          : '## AI';
        parts.push(aiLabel);
      }
      if (date) parts.push(`*${date}*`);
      parts.push('');
      parts.push(m.content);
      parts.push('');
      parts.push('----');
      parts.push('');
    }

    const md = parts.join('\n');
    const blob = new Blob([md], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `chat-${activeSession?.title || 'untitled'}-${Date.now()}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }, [activeSession]);

  const handleStartRename = useCallback(() => {
    setRenameValue(activeSession?.title || '');
    setRenaming(true);
  }, [activeSession]);

  const handleRenameCommit = useCallback(async () => {
    const trimmed = renameValue.trim();
    if (!trimmed || !activeSession || trimmed === activeSession.title || !activeSessionId) { setRenaming(false); return; }
    // WHY optimistic-then-deferred-close: paint the new title immediately, then
    // close the input AFTER the server confirms. Closing before the round-trip
    // can let the server response (which rewrites the whole row) race against
    // the optimistic set and briefly flash the old name. updateSession owns the
    // error toast; setRenaming runs in finally so a failed PATCH still releases
    // the input instead of trapping the user in an editing state.
    useChatStore.setState(state => ({
      sessions: state.sessions.map(ss => ss.session_id === activeSessionId ? { ...ss, title: trimmed } : ss),
    }));
    try {
      await updateSession(activeSessionId, { title: trimmed });
    } finally {
      setRenaming(false);
    }
  }, [renameValue, activeSession, activeSessionId, updateSession]);

  // A "go to parent" affordance to the chat's home entity, rendered in the
  // reserved left slot before the title. INVARIANT: a reference-scoped chat's
  // parent IS the reference (navigate-to-reference), never its owning document.
  // Why: the chat's identity is the entity it was created on (a recurring user
  // rule). A document-session navigates to its owning document. Only shown when
  // that entity differs from the currently open one.
  const isRefSession = !!activeSession?.reference_id;
  const parentDocId = activeSession?.document_id ?? null;
  const showGotoParent = isRefSession
    ? !!activeSession?.reference_id && activeSession.reference_id !== currentReferenceId
    : !!parentDocId && parentDocId !== currentDocumentId;
  // INVARIANT: the change-parent affordance is guarded on isRefSession
  // EXPLICITLY, never on the negation of showGotoParent. Why: a reference-scoped
  // chat's parent is the reference (the rule above); even in its "empty slot"
  // state (ref open) it must never offer a document reparent — that write is a
  // 400 for ref-scoped rows and the header is not the place to surface it.
  // Shown only when the parent IS the open document — the exact state where the
  // goto-parent branch renders the slot empty.
  const showChangeParent = !isRefSession && !!parentDocId && parentDocId === currentDocumentId;

  const handleReparent = useCallback((newDocumentId: string | null) => {
    // updateSession reconciles the server row into the store (title/doc flip on
    // next render); its catch owns the error toast. Both header states derive
    // from activeSession.document_id, so the icon flips to goto-parent as soon
    // as the PATCH lands — nothing is cached in local state.
    if (!activeSessionId) return;
    void updateSession(activeSessionId, { document_id: newDocumentId });
  }, [activeSessionId, updateSession]);

  // Parent label for the goto-parent tooltip. ChatRow-style fallback to the
  // client document tree: the PATCH response serializes document_title=null
  // (the update route carries no titles map — see serialize_session), so a
  // row reconciled from a settings/reparent PATCH would otherwise label the
  // tooltip "Go to " until the next list reload.
  const parentTitle = (isRefSession
    ? activeSession?.reference_title
    : activeSession?.document_title ?? documents.find(d => d.document_id === parentDocId)?.title) ?? '';

  // Ghost (no session yet): a NEW-CHAT plaque. Yellowed to signal "you are
  // composing a fresh chat" — mirrors the notes-thread tint. Left-aligned
  // hint … [search] [sort-toggle]. Search flips the hint into a title-filter
  // input IN PLACE (same row height, controls stay put). The sort toggle is
  // hidden when no document is open (proximity is meaningless without an
  // anchor); the list stays neutral either way.
  if (!activeSessionId) {
    const searching = listFilter !== null;
    return (
      <div className="flex items-center gap-2 px-3 py-2 border-b border-[var(--sticky-yellow-sep)] bg-[var(--sticky-yellow-light)] min-h-[40px]">
        <div className="flex-1 min-w-0">
          {searching ? (
            <FieldInput
              className="doc-rename-input chat-rename-input chat-search-input w-full"
              data-chat-search-input
              value={listFilter}
              placeholder={t('typeAtLeastN', { n: CHAT_LIST_FILTER_MIN })}
              onChange={e => setListFilter(e.target.value)}
              autoFocus
              onKeyDown={e => {
                if (e.key === 'Escape') { e.preventDefault(); setListFilter(null); }
              }}
              // An empty query on blur means the mode was abandoned, not paused.
              onBlur={() => { if (listFilter === '') setListFilter(null); }}
            />
          ) : (
            <span className="block text-sm font-medium text-sticky-ink truncate">{t('chatEmptyHint')}</span>
          )}
        </div>
        <IconButton
          size="sm"
          theme="note"
          className={`ml-auto ${searching ? 'text-accent' : 'text-sticky-ink'}`}
          title={t('chatSearchTitles')}
          onClick={() => setListFilter(searching ? null : '')}
        >
          <Search size={14} />
        </IconButton>
        {currentDocumentId && (
          <IconButton
            size="sm"
            theme="note"
            className={chatSortByProximity ? 'text-accent' : 'text-sticky-ink'}
            title={chatSortByProximity ? t('chatSortByRecency') : t('chatSortByProximity')}
            onClick={() => setChatSortByProximity(currentDocumentId, !chatSortByProximity)}
          >
            {chatSortByProximity ? <Network size={14} /> : <Clock size={14} />}
          </IconButton>
        )}
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1 px-3 py-2 border-b border-border bg-surface min-h-[40px]">
      <Button variant="primary" size="sm" onClick={handleNewChat}>
        <Plus size={13} />
        {t('chatNew')}
      </Button>

      {activeSessionId && (
        // WHY this 22×22 slot (IconButton size="sm" footprint) is ALWAYS rendered
        // between the "+ New chat" button and the title with AT MOST one icon —
        // goto-parent / change-parent / empty: the title's left edge must not
        // shift across states (icon shown / hidden / renaming); a slot that
        // unmounts would make the rename input slide left into its place. The
        // two icon branches are mutually exclusive by construction (goto-parent
        // requires parent ≠ open entity, change-parent requires parent == open
        // document), so the slot never holds two icons.
        <div data-title-slot className="flex items-center justify-center w-[22px] h-[22px] shrink-0">
          {showGotoParent && (
            <IconButton
              size="sm"
              title={t('gotoParentDoc', { title: parentTitle })}
              onClick={() => {
                if (isRefSession) {
                  if (!activeSession?.reference_id) return;
                  if (activeSession.document_id) useUIStore.getState().pinRightPanel(activeSession.document_id, 'chat');
                  emit('navigate-to-reference', { referenceId: activeSession.reference_id, sourceDocId: null });
                  return;
                }
                if (!parentDocId) return;
                // ARCH: keep the current chat open across the jump — the chat is
                // project-scoped; the parent opens as its bare body (the
                // navigate-to-document default), not its remembered reference.
                navigateToDocKeepingChat(parentDocId);
              }}
            >
              <FileText size={14} />
            </IconButton>
          )}
          {showChangeParent && (
            <IconButton
              size="sm"
              title={t('changeChatParent')}
              onClick={e => setParentPickerRect(e.currentTarget.getBoundingClientRect())}
            >
              <FolderUp size={14} />
            </IconButton>
          )}
        </div>
      )}

      <div className="flex-1 min-w-0">
        {renaming ? (
          <FieldInput
            className="doc-rename-input chat-rename-input w-full"
            data-rename-input
            value={renameValue}
            onChange={e => setRenameValue(e.target.value)}
            autoFocus
            onKeyDown={e => {
              if (e.key === 'Enter') { e.preventDefault(); handleRenameCommit(); }
              if (e.key === 'Escape') { setRenaming(false); }
            }}
            onBlur={handleRenameCommit}
          />
        ) : (
          // ARCH: the title renders as STATIC text (the session
          // dropdown was removed — switching happens from the empty-screen list).
          // Same typography as the former dropdown trigger, non-interactive.
          // Rename entry point is DOUBLE-CLICK only — the pencil icon was removed;
          // the native tooltip carries the hint so the affordance stays discoverable.
          // NO horizontal padding (ref-plaque rename contract, see .ref-rename-input):
          // the edit input renders input text at padding 0, so the static span must
          // sit at content-left 0 too — any side padding here reappears as a
          // text shift on entering edit mode in engines that inset input text.
          <span
            data-chat-title
            className="block text-sm font-medium text-text truncate"
            title={`${activeSession?.title || t('newChat')} — ${t('chatRenameHint')}`}
            onDoubleClick={handleStartRename}
          >
            {activeSession?.title || t('newChat')}
          </span>
        )}
      </div>

      {activeSessionId && (
        <>
          <Popover
            align="right"
            placement="bottom"
            keyboardNav={false}
            panelClassName="min-w-[120px]"
            trigger={
              <IconButton size="sm" title={t('exportChat')}>
                <Download size={14} />
              </IconButton>
            }
          >
            {({ close }) => (
              <>
                <div
                  role="button"
                  tabIndex={0}
                  className="block w-full text-left px-3 py-2 text-sm cursor-pointer text-text hover:bg-surface2"
                  onClick={() => { handleExportMarkdown(); close(); }}
                  onKeyDown={e => { if (e.key === 'Enter') { handleExportMarkdown(); close(); } }}
                >
                  {t('exportAsMarkdown')}
                </div>
                <div
                  role="button"
                  tabIndex={0}
                  className="block w-full text-left px-3 py-2 text-sm cursor-pointer text-text hover:bg-surface2"
                  onClick={() => { handleExportJson(); close(); }}
                  onKeyDown={e => { if (e.key === 'Enter') { handleExportJson(); close(); } }}
                >
                  {t('exportAsJson')}
                </div>
              </>
            )}
          </Popover>
          <IconButton
            size="sm"
            danger
            filled={deleteAction.armed}
            title={t('deleteChat')}
            onClick={() => deleteAction.handleClick(async () => {
              if (activeSessionId) await deleteSession(activeSessionId);
            })}
            onMouseLeave={deleteAction.disarm}
          >
            <Trash2 size={14} />
          </IconButton>
        </>
      )}

      {parentPickerRect && (
        // Same reference-mode picker the ReferencesPanel mounts (no `doc` — a
        // chat is not in the document tree, so there is no cycle to exclude).
        // currentDocumentId = the session's parent so the picker marks it
        // "current"; onMoved PATCHes document_id (null = project-level).
        <ParentPickerPopup
          currentDocumentId={parentDocId}
          anchorRect={parentPickerRect}
          onClose={() => setParentPickerRect(null)}
          onMoved={handleReparent}
        />
      )}
    </div>
  );
}
