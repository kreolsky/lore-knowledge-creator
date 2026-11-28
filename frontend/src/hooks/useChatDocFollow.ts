/** Chat mode's right document: follows the agent's writes in the active chat unless pinned;
 * any navigation of it that the follow did not make pins it. */

import { useEffect, useRef } from 'react';
import { useNavigate, type NavigateFunction } from 'react-router-dom';
import { useAppStore } from '../store/app-store';
import { useChatStore } from '../store/chat-store';
import { useUIStore } from '../store/ui-store';
import { openDocument } from '../navigation/open-document';
import { selectFollowTarget, followTargetDocId } from '../components/chat/follow-target';

// The document the follow is navigating to — the pin effect lets exactly this change through.
let expectedDocId: string | null = null;
// A follow target not yet in the project's document list (a fresh create_document result can
// land before the list update) — opened once the list carries it.
let pendingDocId: string | null = null;

/** The current document change was made by the follow, not by the user (read before the pin effect runs). */
export function isFollowNavigation(docId: string | null): boolean {
  return docId !== null && docId === expectedDocId;
}

function followTo(docId: string, navigate: NavigateFunction): void {
  const state = useAppStore.getState();
  pendingDocId = null;
  if (state.currentDocument?.document_id === docId) return;
  if (!state.documents.some(d => d.document_id === docId)) { pendingDocId = docId; return; }
  expectedDocId = docId;
  openDocument(docId, { navigate });
}

/** Unpin: follow again, and jump to the active chat's newest write if there is one. */
export function unpinChatDoc(navigate: NavigateFunction): void {
  useUIStore.getState().setChatDocPinned(false);
  const docId = followTargetDocId(selectFollowTarget(useChatStore.getState().conversation));
  if (docId) followTo(docId, navigate);
}

export function useChatDocFollow(chatMode: boolean): void {
  const navigate = useNavigate();
  const target = useChatStore(s => selectFollowTarget(s.conversation));
  const currentDocId = useAppStore(s => s.currentDocument?.document_id ?? null);
  const documents = useAppStore(s => s.documents);
  const seenTargetRef = useRef<string | null>(null);
  const lastChatModeDocRef = useRef<string | null>(null);

  useEffect(() => {
    const previous = seenTargetRef.current;
    seenTargetRef.current = target;
    if (!chatMode || !target || target === previous) return;
    if (useChatStore.getState().streaming === null) return; // INVARIANT: follow fires only on a NEW write that lands while a turn is streaming.
    // Why: opening an older chat or reloading must not move the document under the user —
    // the panel follows what the agent is writing now, not what it wrote before.
    if (useUIStore.getState().chatDocPinned) return;
    const docId = followTargetDocId(target);
    if (docId) followTo(docId, navigate);
  }, [chatMode, target, navigate]);

  useEffect(() => {
    if (!chatMode || !pendingDocId || useUIStore.getState().chatDocPinned) return;
    if (documents.some(d => d.document_id === pendingDocId)) followTo(pendingDocId, navigate);
  }, [chatMode, documents, navigate]);

  // Any change of the chat-mode document the follow did not make pins it — the tree picker,
  // a link, a search result (which also shows the document tab), back/forward, a return from docs mode on another document.
  // WHY from-nothing is exempt: a reload commits the URL document after a null window.
  useEffect(() => {
    if (!chatMode) return;
    const previous = lastChatModeDocRef.current;
    lastChatModeDocRef.current = currentDocId;
    if (currentDocId === expectedDocId) { expectedDocId = null; return; }
    if (previous === null || currentDocId === previous) return;
    const ui = useUIStore.getState();
    ui.setChatDocPinned(true);
    // WHY show the document tab: a search-tab result opened in chat mode is otherwise invisible.
    ui.setChatModeRightTab('doc');
  }, [chatMode, currentDocId]);
}
