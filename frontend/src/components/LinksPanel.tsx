/**
 * Links panel: incoming and outgoing relations of the open document or reference.
 *
 * Both directions come from GET /documents/{id}/edges (see SYSTEM: edges), grouped
 * by kind, one section per (direction, kind) group; a stored outgoing edge to a
 * deleted target is shown as broken. Notes and the viewer's own chats are included
 * when the project setting `links_show_sessions` is on. Rows show the shared
 * right-panel hover preview. External http(s) links are not graph nodes and stay a
 * client parse of live editor content.
 * Store slices: currentDocument, currentReference, currentProject; ui documents (ref open mode).
 * Events consumed: collab-backlinks-changed, editor-doc-changed, ws:document_created,
 * ws:reference_created, ws:reference_deleted, ws:document_deleted, collab-note-created,
 * collab-note-deleted.
 * Events emitted: navigate-to-document, navigate-to-reference, open-notes, open-right-panel.
 */

import { useState, useEffect, useCallback } from 'react';
import type { ReactNode } from 'react';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { useChatStore } from '../store/chat-store';
import { readRefOpenMode, refIsScope } from '../store/ui-store/documents-slice';
import { apiClient } from '../api/client';
import { emit } from '../events';
import { useEvent } from '../hooks/useEvent';
import { parseExternalLinks, type ExternalLink as ExternalLinkItem } from '../utils/outgoing-links';
import { useEditorContent } from '../editor/active-editor';
import { useRightPanelHoverPreview } from '../hooks/useRightPanelHoverPreview';
import { useDocumentPreview } from '../hooks/useDocumentPreview';
import { useReferencePreview } from '../hooks/useReferencePreview';
import { HoverPreviewPopup } from './HoverPreviewPopup';
import {
  FileText, Paperclip, ExternalLink, ArrowDownLeft, ArrowUpRight, Link2, SquareStack, MessageSquare, StickyNote,
} from 'lucide-react';
import { useTranslation } from '../i18n';
import { Button, SectionHeader, PanelLoading } from './ui';

type EdgeKind = 'mention' | 'transclusion' | 'attachment' | 'note' | 'chat-context';

interface EdgeItem {
  direction: 'in' | 'out';
  kind: EdgeKind;
  document_id?: string;
  session_id?: string;
  title?: string;
  label?: string;
  source_type?: 'doc' | 'ref';
  broken?: boolean;
}

type KindLabelKey = 'edgeKindMention' | 'edgeKindTransclusion' | 'edgeKindAttachment' | 'edgeKindNote' | 'edgeKindChat';

const KIND_ORDER: { kind: EdgeKind; labelKey: KindLabelKey; icon: typeof FileText }[] = [
  { kind: 'mention', labelKey: 'edgeKindMention', icon: Link2 },
  { kind: 'transclusion', labelKey: 'edgeKindTransclusion', icon: SquareStack },
  { kind: 'attachment', labelKey: 'edgeKindAttachment', icon: Paperclip },
  { kind: 'note', labelKey: 'edgeKindNote', icon: StickyNote },
  { kind: 'chat-context', labelKey: 'edgeKindChat', icon: MessageSquare },
];

// WHY: the same default as the server (mention,transclusion,attachment); notes and
// chats are opt-in, so the project flag widens the request instead of filtering client-side.
const KINDS_WITH_SESSIONS = 'mention,transclusion,attachment,note,chat-context';

function edgeKey(e: EdgeItem): string {
  return `${e.direction}:${e.kind}:${e.document_id ?? e.session_id}`;
}

export function LinksPanel() {
  const currentDocument = useAppStore(s => s.currentDocument);
  const rawReference = useAppStore(s => s.currentReference);
  // Panel quick preview: the Links tab is the DOCUMENT's. The reference-first
  // resolution below still holds for the scope modes (center/split); a previewed
  // ref reads as null through the ONE projection, matching the TOC/notes/tree.
  const refOpenMode = useUIStore(s => readRefOpenMode(s.documents[currentDocument?.document_id ?? ''], s.compactLayout));
  const currentReference = refIsScope(refOpenMode) ? rawReference : null;
  // INVARIANT: a reference shows its OWN links/backlinks, not its parent document's.  Why: while a ref is open both currentDocument (parent) and currentReference are set; resolving reference-first shows the ref's own links, matching the canonical activeItem.
  const documentId = currentReference?.reference_id ?? currentDocument?.document_id;
  // Active entity's saved content — reference-first, same priority as documentId.
  const activeContent = currentReference?.content ?? currentDocument?.content ?? '';
  const { t } = useTranslation();
  const getEditorContent = useEditorContent();

  // ── Graph edges (both directions) ──
  // Project-wide setting (ProjectSettingsPanel), shared by every member.
  const showSessions = useAppStore(s => s.currentProject?.links_show_sessions ?? false);
  const [edges, setEdges] = useState<EdgeItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const fetchEdges = useCallback(() => {
    if (!documentId) { setEdges([]); setLoading(false); return; }
    setError(false);
    const kindsParam = showSessions ? `&kinds=${KINDS_WITH_SESSIONS}` : '';
    apiClient
      .get(`/documents/${documentId}/edges?direction=both${kindsParam}`)
      .then((data: { edges: EdgeItem[] }) => setEdges(data.edges))
      .catch(() => { setError(true); })
      .finally(() => setLoading(false));
  }, [documentId, showSessions]);

  useEffect(() => { setLoading(true); }, [documentId]);
  useEffect(() => { fetchEdges(); }, [fetchEdges]);

  // Stored kinds: the flush emits for the source AND its targets.
  useEvent('collab-backlinks-changed', fetchEdges);
  // Projected kinds refresh on the existing lifecycle events — only when the event
  // concerns this panel's document (attachment host) or one of its peers.
  const isPeer = useCallback((id: string) => edges.some(e => e.document_id === id), [edges]);
  useEvent('ws:document_created', useCallback(({ parent_id: parentId, is_reference: isRef }) => {
    if (isRef && parentId === documentId) fetchEdges();
  }, [documentId, fetchEdges]));
  useEvent('ws:reference_created', useCallback(({ document_id: hostId }) => {
    if (hostId === documentId) fetchEdges();
  }, [documentId, fetchEdges]));
  useEvent('ws:reference_deleted', useCallback(({ reference_id: refId }) => {
    if (isPeer(refId)) fetchEdges();
  }, [isPeer, fetchEdges]));
  useEvent('ws:document_deleted', useCallback(({ document_id: docId }) => {
    if (isPeer(docId)) fetchEdges();
  }, [isPeer, fetchEdges]));
  // Note frames arrive only for joined entities, i.e. the open document.
  useEvent('collab-note-created', useCallback(() => { if (showSessions) fetchEdges(); }, [showSessions, fetchEdges]));
  useEvent('collab-note-deleted', useCallback(() => { if (showSessions) fetchEdges(); }, [showSessions, fetchEdges]));

  // ── External links (client parse) ──
  const [externalLinks, setExternalLinks] = useState<ExternalLinkItem[]>(() => parseExternalLinks(activeContent));

  // WHY: on entity switch parse from the store's saved content, not the live editor
  // view. Why: the CM6 editor remount is deferred (useDeferredValue in Editor.tsx) and
  // reference content loads async via collab, so reading the editor right after a
  // switch returns the PREVIOUS entity's text.
  useEffect(() => {
    setExternalLinks(parseExternalLinks(activeContent));
  }, [activeContent, documentId]);

  const refreshFromEditor = useCallback(() => {
    setExternalLinks(parseExternalLinks(getEditorContent(activeContent)));
  }, [getEditorContent, activeContent]);
  useEvent('editor-doc-changed', refreshFromEditor);

  // ── Hover preview ──
  const hover = useRightPanelHoverPreview();
  const [hoverEdge, setHoverEdge] = useState<EdgeItem | null>(null);
  const hoverDocId = hoverEdge && !hoverEdge.session_id && hoverEdge.source_type !== 'ref' ? hoverEdge.document_id! : null;
  const hoverRefId = hoverEdge && !hoverEdge.session_id && hoverEdge.source_type === 'ref' ? hoverEdge.document_id! : null;
  const docPreview = useDocumentPreview(hoverDocId);
  const refPreview = useReferencePreview(hoverRefId);
  // WHY: notes/chats preview their own edge label — the row already carries it, no fetch.
  const hoverBody = hoverEdge?.session_id
    ? { content: hoverEdge.label, imageUrl: undefined, error: false, loading: false }
    : hoverRefId
      ? { content: refPreview.preview?.content, imageUrl: refPreview.preview?.imageUrl, error: refPreview.error, loading: refPreview.loading }
      : { content: docPreview.content, imageUrl: undefined, error: docPreview.error, loading: docPreview.loading };

  // ── Handlers ──
  const handleEdgeClick = (e: EdgeItem) => {
    if (e.kind === 'note') {
      emit('open-notes', { threadId: e.session_id });
      return;
    }
    if (e.kind === 'chat-context') {
      // Ruling: the chat is the viewer's own and in this project — open it in the
      // right-panel chat beside the current document.
      useChatStore.getState().setActiveSession(e.session_id!);
      useUIStore.getState().setRightPanelTab(useAppStore.getState().currentDocument?.document_id ?? null, 'chat');
      emit('open-right-panel');
      return;
    }
    if (e.source_type === 'ref') {
      emit('navigate-to-reference', { referenceId: e.document_id!, via: 'links_panel' });
    } else {
      emit('navigate-to-document', { documentId: e.document_id!, via: 'links_panel' });
    }
  };

  const onKey = (ev: React.KeyboardEvent<HTMLDivElement>) => {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); ev.currentTarget.click(); }
  };

  const renderEdge = (e: EdgeItem, Icon: typeof FileText) => {
    const label = e.session_id ? (e.label || t('untitledLink')) : (e.broken ? t('brokenLink') : e.title);
    return (
      <div
        key={edgeKey(e)}
        role="button"
        tabIndex={e.broken ? -1 : 0}
        aria-disabled={e.broken || undefined}
        title={e.broken ? t('brokenLinkHint') : undefined}
        className={`doc-item doc-item--right-panel${e.broken ? ' line-through text-text-dim' : ''}`}
        onClick={e.broken ? undefined : () => handleEdgeClick(e)}
        onKeyDown={onKey}
        onMouseEnter={e.broken ? undefined : ev => { setHoverEdge(e); hover.handleHover(ev.currentTarget); }}
        onMouseLeave={e.broken ? undefined : hover.handleHoverLeave}
      >
        <span className="w-3" />
        <span className="doc-icon"><Icon size={14} /></span>
        <span className="doc-label">
          {label}{e.source_type === 'ref' ? ` ${t('refBadge')}` : ''}
        </span>
      </div>
    );
  };

  // One section per non-empty (direction, kind) group; an empty direction keeps ONE
  // section with its empty text, so empty stays distinct from error.
  const renderDirection = (direction: 'in' | 'out', first: boolean) => {
    const dirTitle = t(direction === 'in' ? 'incomingLinks' : 'outgoingLinks');
    const dirIcon: ReactNode = direction === 'in' ? <ArrowDownLeft size={11} /> : <ArrowUpRight size={11} />;
    const inDir = edges.filter(e => e.direction === direction);
    const groups = KIND_ORDER
      .map(k => ({ ...k, items: inDir.filter(e => e.kind === k.kind) }))
      .filter(g => g.items.length > 0);
    if (groups.length === 0) {
      return (
        <>
          <SectionHeader first={first} icon={dirIcon} title={dirTitle} />
          <p className="text-ui-base text-text-dim text-center py-2 px-2">
            {t(direction === 'in' ? 'noDocsLinkHere' : 'noOutgoingLinks')}
          </p>
        </>
      );
    }
    return groups.map(({ kind, labelKey, icon, items }, i) => (
      <div key={`${direction}-${kind}`}>
        <SectionHeader first={first && i === 0} icon={dirIcon} title={dirTitle} suffix={t(labelKey)} />
        {items.map(e => renderEdge(e, icon))}
      </div>
    ));
  };

  if (loading) {
    return <PanelLoading />;
  }

  return (
    <div className="flex flex-col h-full overflow-auto py-1 px-1.5">
      {error && (
        <p className="text-ui-base text-text-dim text-center py-4 px-2">
          {t('failedToLoadLinks')}{' '}
          <Button type="button" variant="ghost" size="sm" onClick={fetchEdges}>{t('retry')}</Button>
        </p>
      )}

      {!error && (
        <>
          {renderDirection('in', true)}
          {renderDirection('out', false)}
        </>
      )}

      {externalLinks.length > 0 && (
        <div>
          <SectionHeader icon={<ArrowUpRight size={11} />} title={t('outgoingLinks')} suffix={t('externalLinks')} />
          {externalLinks.map((link, i) => (
            <div
              key={`${link.target}-${i}`}
              role="button"
              tabIndex={0}
              className="doc-item doc-item--right-panel"
              onClick={() => window.open(link.target, '_blank', 'noopener')}
              onKeyDown={onKey}
            >
              <span className="w-3" />
              <span className="doc-icon"><ExternalLink size={14} /></span>
              <span className="doc-label">{link.label}</span>
            </div>
          ))}
        </div>
      )}
      <HoverPreviewPopup
        hover={hover}
        title={hoverEdge ? (hoverEdge.session_id ? hoverEdge.label || t('untitledLink') : hoverEdge.title) : undefined}
        content={hoverBody.content}
        imageUrl={hoverBody.imageUrl}
        error={hoverBody.error}
        loading={hoverBody.loading}
      />
    </div>
  );
}
