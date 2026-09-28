/** Top-pinned, scrolling list of existing chats shown on the empty chat screen. Renders ALL sessions (project-wide, pure time sort); the container scrolls natively. */

import { useState, useEffect, useCallback, useRef } from 'react';
import { useChatStore } from '../../store/chat-store';
import { useAppStore } from '../../store/app-store';
import { PillList } from '../ui';
import { ChatRow } from './ChatRow';
import { useSortedSessions, filterSessionsByTitle } from './useSortedSessions';
import { useTranslation } from '../../i18n';
import { HoverPreviewPopup } from '../HoverPreviewPopup';
import { useDocumentPreview } from '../../hooks/useDocumentPreview';
import { useLastMessagePreview } from '../../hooks/useLastMessagePreview';
import { useReferencePreview } from '../../hooks/useReferencePreview';
import { useRightPanelHoverPreview } from '../../hooks/useRightPanelHoverPreview';

export function ChatEmptyList() {
  // Pure last-activity sort of ALL sessions.
  const allSorted = useSortedSessions();
  const listFilter = useChatStore(s => s.listFilter);
  const sorted = filterSessionsByTitle(allSorted, listFilter);
  const { t } = useTranslation();
  const activeSessionId = useChatStore(s => s.activeSessionId);
  const setActiveSession = useChatStore(s => s.setActiveSession);
  const deleteSession = useChatStore(s => s.deleteSession);
  const updateSession = useChatStore(s => s.updateSession);
  const documentId = useAppStore(s => s.currentDocument?.document_id);

  const containerRef = useRef<HTMLDivElement>(null);

  // INVARIANT: scroll back to the top ONLY on a scope switch (documentId), never on a
  // list-length change. Why: deleting a chat far down the list must keep the current
  // scroll position — rows below rise to fill the deleted row's gap instead of the view
  // snapping to the start.
  useEffect(() => {
    if (containerRef.current) containerRef.current.scrollTop = 0;
  }, [documentId]);

  const handleSelect = useCallback((id: string) => {
    setActiveSession(id);
  }, [setActiveSession]);

  const handleDelete = useCallback((id: string) => {
    void deleteSession(id);
  }, [deleteSession]);

  // Optimistic title paint, then the PATCH; updateSession owns the error toast and
  // the server row rewrite (same contract as ChatHeader's rename).
  const handleRename = useCallback(async (id: string, title: string) => {
    useChatStore.setState(state => ({
      sessions: state.sessions.map(ss => ss.session_id === id ? { ...ss, title } : ss),
    }));
    await updateSession(id, { title });
  }, [updateSession]);

  // ONE popup at list level, positioned to the LEFT of .right-panel with above/below
  // flip (shared right-panel positioning via useRightPanelHoverPreview — identical to
  // NotesPanel / ReferencesPanel). Two hover targets swap the SAME popup's content via
  // a discriminated `hoverKind`:
  //   - 'session' (whole plaque): the chat's LAST MESSAGE, lazily fetched per hovered
  //     row (SYSTEM: chat-last-message-preview). The session list is content-free for
  //     AI chats by design (perf) — we fetch one hovered session on demand.
  //   - 'doc' (parent-doc label): the owning document content (SYSTEM: document-preview).
  //   - 'ref' (a reference-scoped chat's parent IS the reference): the reference body OR
  //     image, via the SHARED reference-preview scheme (SYSTEM: reference-preview,
  //     useReferencePreview → content | imageUrl) — same path NotesPanel/ReferencesPanel
  //     use, so image references render as images, not empty document content.
  //     The pill's mouse-enter already positioned the popup; the nested label only swaps
  //     content (no reposition), and its mouse-leave restores the last-message content.
  const [hoverKind, setHoverKind] = useState<'session' | 'doc' | 'ref'>('session');
  const [hoverDocId, setHoverDocId] = useState<string | null>(null);
  const [hoverRefId, setHoverRefId] = useState<string | null>(null);
  const [hoverSessionId, setHoverSessionId] = useState<string | null>(null);
  const { content: docContent, error: docError, loading: docLoading } = useDocumentPreview(hoverKind === 'doc' ? hoverDocId : null);
  const { preview: refPreview, error: refError, loading: refLoading } = useReferencePreview(hoverKind === 'ref' ? hoverRefId : null);
  const { content: sessionContent } = useLastMessagePreview(hoverKind === 'session' ? hoverSessionId : null);
  const hoverContent = hoverKind === 'doc'
    ? docContent
    : hoverKind === 'ref' ? (refPreview?.content ?? '') : sessionContent;
  const hoverImageUrl = hoverKind === 'ref' ? refPreview?.imageUrl : undefined;
  // Only the hovered kind's hook holds a non-null id, so the other reports neither error
  // nor loading. The 'session' kind has no error/loading channel (useLastMessagePreview).
  const hoverError = hoverKind === 'doc' ? docError : hoverKind === 'ref' ? refError : false;
  const hoverLoading = hoverKind === 'doc' ? docLoading : hoverKind === 'ref' ? refLoading : false;
  const hover = useRightPanelHoverPreview();

  const handleBodyHover = useCallback((sessionId: string, el: HTMLElement) => {
    setHoverKind('session');
    setHoverSessionId(sessionId);
    hover.handleHover(el);
  }, [hover.handleHover]);

  // Parent-label enter/leave only swap content (the pill enter owns positioning).
  // A ref-scoped chat's parent IS the reference → route to the shared reference preview
  // (image-aware); a document-session routes to the document-content preview.
  const handleParentHover = useCallback((id: string, _el: HTMLElement, isRef: boolean) => {
    if (isRef) {
      setHoverRefId(id);
      setHoverKind('ref');
    } else {
      setHoverDocId(id);
      setHoverKind('doc');
    }
  }, []);
  const handleParentHoverLeave = useCallback(() => {
    setHoverKind('session');
  }, []);

  // No chats: render nothing — just an empty area, no controls.
  // The hint lives in ChatHeader's ghost-state panel; avoid duplicating it.
  if (sorted.length === 0) {
    // A live filter that matched nothing says so — an empty list must not read
    // as "no chats" (empty state ≠ filtered-out state).
    if (sorted !== allSorted && allSorted.length > 0) {
      return (
        // Same typography/placement as SearchPanel's empty state.
        <div className="flex-1">
          <p className="text-ui-base text-text-dim text-center py-4 px-2">{t('noMatchesFound')}</p>
        </div>
      );
    }
    return <div className="flex-1" />;
  }

  return (
    <div className="flex-1 flex flex-col overflow-hidden relative">
      <PillList ref={containerRef} className="min-h-0 list-scroll">
        {sorted.map((s) => (
          <ChatRow
            key={s.session_id}
            session={s}
            isActive={s.session_id === activeSessionId}
            onSelect={handleSelect}
            onDelete={handleDelete}
            onRename={handleRename}
            onParentHover={handleParentHover}
            onParentHoverLeave={handleParentHoverLeave}
            onBodyHover={handleBodyHover}
            onBodyHoverLeave={hover.handleHoverLeave}
          />
        ))}
      </PillList>
      <HoverPreviewPopup
        hover={hover}
        content={hoverContent}
        imageUrl={hoverImageUrl}
        error={hoverError}
        loading={hoverLoading}
      />
    </div>
  );
}
