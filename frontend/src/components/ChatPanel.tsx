/**
 * AI Chat panel — right panel tab for LLM conversations.
 *
 * Composes ChatHeader (session/model selectors), MessageList (message tree),
 * and ChatInput (send/stop/mic). Uses separate chat-store (not app-store).
 *
 * The entire panel is a drop zone for images — drops anywhere add to pending
 * images in chat-store, displayed in ChatInput's preview strip.
 */

import { useEffect } from 'react';
import { ChatHeader } from './chat/ChatHeader';
import { MessageList } from './chat/MessageList';
import { ChatInput } from './chat/ChatInput';
import { ChatClarifyPopover } from './chat/ChatClarifyPopover';
import { ChatUnavailableNotice, useChatUnavailable } from './chat/ChatUnavailableNotice';
import { useTranslation } from '../i18n';
import { useChatStore } from '../store/chat-store';
import { useAppStore } from '../store/app-store';
import { useGhostContextWarm } from '../chat/use-ghost-context';
import { useImageDropHandlers } from '../hooks/useImageDropHandlers';
import { registerLogoutHandler } from '../store/logout-handlers';
import {
  isChatScopeLoaded,
  markChatScopeLoaded,
  resetChatScopeTracker,
} from '../chat/scope-tracker';

/**
 * Reset the module-level scope tracker so a re-login in the same
 * tab forces the scope-effect to treat the scope as changed and call loadSessions.
 * Without this, the tracker survives a soft logout and a same-scope reopen
 * (scopeChanged === false) skips loadSessions entirely — leaving the panel on whatever
 * the store holds (empty after reset, but the project's chats would never load until a
 * navigation). Registered into the logout registry at module load.
 */
registerLogoutHandler(resetChatScopeTracker);

export function ChatPanel() {
  const { t } = useTranslation();
  const projectId = useAppStore(s => s.currentProject?.project_id);
  const documentId = useAppStore(s => s.currentDocument?.document_id);
  const loadSessions = useChatStore(s => s.loadSessions);
  const addPendingImage = useChatStore(s => s.addPendingImage);
  const chatUnavailable = useChatUnavailable();
  const { isDragging, dragHandlers } = useImageDropHandlers({
    getPending: () => useChatStore.getState().pendingImages,
    addImage: addPendingImage,
    enabled: !chatUnavailable,
  });

  // ARCH: the active chat is PROJECT-SCOPED and
  // persists across document navigation. loadSessions fires on PROJECT change only —
  // switching documents within a project keeps the same chat open (no reload, no
  // re-resolve). The tracker holds only the project so a doc switch is a no-op.
  // WHY: the last scope this effect loaded is tracked at MODULE level
  // (chat/scope-tracker), NOT in a component-local ref. Why: switching the right
  // panel tab unmounts ChatPanel, which would reset a useRef; on return the
  // effect would see a spurious scope change. A module-level tracker survives
  // remount, so a tab round-trip with an unchanged project is a no-op.
  // INVARIANT: documentId is read in-deps so it is current at the moment the
  // project changes (project + doc change together), but the load is gated on the
  // PROJECT only.  Why: the load gates on PROJECT (not doc) so an unchanged-project tab round-trip is a no-op; documentId is in-deps only to be current at project-change. "Chat with Reference" marks the scope loaded itself so this
  // effect skips its own reload (which would restore an old chat and overwrite the
  // null/ghost chat openChatWithReference just set).
  const activeSessionId = useChatStore(s => s.activeSessionId);
  useEffect(() => {
    if (!activeSessionId) {
      useChatStore.setState({ pendingInputFocus: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Ghost auto-context: DERIVED, not stored. The warm-effect fills the first-circle
  // cache for the open entity (+ manual picks) and syncs the ghost base key (which
  // resets deltas on an entity change). useGhostChatContext (in ChatInput) reads the
  // derived value reactively. See chat/use-ghost-context.ts.
  useGhostContextWarm();
  useEffect(() => {
    if (!projectId || !documentId) return;
    // Load once per project; document switches within a project are a no-op.
    if (isChatScopeLoaded(projectId)) return;
    markChatScopeLoaded(projectId);
    loadSessions(projectId, documentId);
  }, [projectId, documentId, loadSessions]);



  return (
    <div
      // data-testid: a double-blink drive reads this node's identity to assert the
      // panel is NOT remounted on an in-app document switch.
      data-testid="chat-panel-root"
      className="flex flex-col h-full relative"
      {...dragHandlers}
    >
      <ChatHeader />
      <ChatClarifyPopover />
      <MessageList />
      {/* No model for this user ⇒ nothing to send to: the whole composer (context,
          persona, input, auto-send, mic) gives way to the notice. History stays readable. */}
      {chatUnavailable ? <ChatUnavailableNotice /> : <ChatInput />}
      {isDragging && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-bg/60 border-2 border-dashed border-accent pointer-events-none">
          <span className="text-text-muted text-sm">{t('dropImagesHere')}</span>
        </div>
      )}
    </div>
  );
}
