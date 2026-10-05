/**
 * Combined links panel: incoming (backlinks) and outgoing links in one tab.
 *
 * Incoming section fetches from GET /documents/{id}/backlinks.
 * Outgoing section parses links from editor content.
 * Store slices: currentDocument, currentReference. Events consumed: collab-backlinks-changed, editor-doc-changed.
 * Events emitted: navigate-to-document, navigate-to-reference.
 */

import { useState, useEffect, useCallback } from 'react';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { readRefOpenMode, refIsScope } from '../store/ui-store/documents-slice';
import { apiClient } from '../api/client';
import { emit } from '../events';
import { useEvent } from '../hooks/useEvent';
import { parseOutgoingLinks, type OutgoingLink } from '../utils/outgoing-links';
import { useEditorContent } from '../editor/active-editor';
import { FileText, Paperclip, ExternalLink, ArrowDownLeft } from 'lucide-react';
import { useTranslation } from '../i18n';
import { Button, SectionHeader, PanelLoading } from './ui';

interface BacklinkItem {
  document_id?: string;
  reference_id?: string;
  source_type?: 'doc' | 'ref';
  title: string;
  updated_at: string | null;
}

const OUTGOING_SECTIONS: { type: OutgoingLink['type']; titleKey: 'documents' | 'references' | 'externalLinks'; icon: typeof FileText }[] = [
  { type: 'doc', titleKey: 'documents', icon: FileText },
  { type: 'ref', titleKey: 'references', icon: Paperclip },
  { type: 'ext', titleKey: 'externalLinks', icon: ExternalLink },
];

export function LinksPanel() {
  const currentDocument = useAppStore(s => s.currentDocument);
  const rawReference = useAppStore(s => s.currentReference);
  // Panel quick preview: the Links tab is the DOCUMENT's. The reference-first
  // resolution below still holds for the scope modes (center/split); a previewed
  // ref reads as null through the ONE projection, matching the TOC/notes/tree.
  const refOpenMode = useUIStore(s => readRefOpenMode(s.documents[currentDocument?.document_id ?? ''], s.compactLayout));
  const currentReference = refIsScope(refOpenMode) ? rawReference : null;
  // INVARIANT: a reference shows its OWN links/backlinks, not its parent document's.  Why: while a ref is open both currentDocument (parent) and currentReference are set; resolving reference-first shows the ref's own links, matching the canonical activeItem.
  // Both currentDocument (parent) and currentReference are set while a ref is
  // the scope, so resolve the active entity reference-first — matching the
  // canonical activeItem in Editor.tsx (currentReference || currentDocument).
  // Why: using currentDocument here made an open reference display the parent
  // doc's backlinks.
  const documentId = currentReference?.reference_id ?? currentDocument?.document_id;
  // Active entity's saved content from the store — reference-first, same priority
  // as documentId. Used as the authoritative source for outgoing links on entity
  // switch.
  const activeContent = currentReference?.content ?? currentDocument?.content ?? '';
  const { t } = useTranslation();
  const getEditorContent = useEditorContent();

  // ── Incoming (backlinks) state ──
  const [backlinks, setBacklinks] = useState<BacklinkItem[]>([]);
  const [backlinksLoading, setBacklinksLoading] = useState(true);
  const [backlinksError, setBacklinksError] = useState(false);

  const fetchBacklinks = useCallback(() => {
    if (!documentId) { setBacklinks([]); setBacklinksLoading(false); return; }
    setBacklinksLoading(true);
    setBacklinksError(false);
    apiClient
      .get(`/documents/${documentId}/backlinks`)
      .then((data: { backlinks: BacklinkItem[] }) => setBacklinks(data.backlinks))
      .catch(() => { setBacklinksError(true); })
      .finally(() => setBacklinksLoading(false));
  }, [documentId]);

  useEffect(() => { fetchBacklinks(); }, [fetchBacklinks]);
  useEvent('collab-backlinks-changed', useCallback(() => fetchBacklinks(), [fetchBacklinks]));

  // ── Outgoing links state ──
  const [outgoingLinks, setOutgoingLinks] = useState<OutgoingLink[]>(() => parseOutgoingLinks(activeContent));

  // WHY: on entity switch parse outgoing links from the store's saved content,
  // not the live editor view. Why: the CM6 editor remount is deferred (useDeferredValue
  // in Editor.tsx) and reference content loads async via collab, so reading the editor
  // right after a switch returns the PREVIOUS entity's text — a reference would show its
  // parent document's outgoing links. The store content is correct and synchronously
  // available the moment the entity opens.
  useEffect(() => {
    setOutgoingLinks(parseOutgoingLinks(activeContent));
  }, [activeContent, documentId]);

  // Live edits: editor-doc-changed only fires on actual content edits, so the live editor
  // view is the right (freshest) source here. Fall back to store content if no view.
  const refreshFromEditor = useCallback(() => {
    setOutgoingLinks(parseOutgoingLinks(getEditorContent(activeContent)));
  }, [getEditorContent, activeContent]);
  useEvent('editor-doc-changed', refreshFromEditor);

  // ── Handlers ──
  const handleBacklinkClick = (bl: BacklinkItem) => {
    if (bl.source_type === 'ref') {
      emit('navigate-to-reference', { referenceId: bl.reference_id! });
    } else {
      emit('navigate-to-document', { documentId: bl.document_id! });
    }
  };

  const handleOutgoingClick = (link: OutgoingLink) => {
    if (link.type === 'doc') {
      emit('navigate-to-document', { documentId: link.target });
    } else if (link.type === 'ref') {
      emit('navigate-to-reference', { referenceId: link.target.replace(/^ref:/, '') });
    } else {
      window.open(link.target, '_blank', 'noopener');
    }
  };

  const hasOutgoing = outgoingLinks.length > 0;

  if (backlinksLoading) {
    return <PanelLoading />;
  }

  return (
    <div className="flex flex-col h-full overflow-auto py-1 px-1.5">
      {/* ── Incoming (backlinks) ── */}
      <SectionHeader first icon={<ArrowDownLeft size={11} />} title={t('incomingLinks')} />

      {backlinksError && (
        <p className="text-ui-base text-text-dim text-center py-4 px-2">
          {t('failedToLoadBacklinks')}{' '}
          <Button type="button" variant="ghost" size="sm" onClick={fetchBacklinks}>{t('retry')}</Button>
        </p>
      )}
      {!backlinksError && backlinks.length === 0 && (
        <p className="text-ui-base text-text-dim text-center py-2 px-2">{t('noDocsLinkHere')}</p>
      )}
      {!backlinksError && backlinks.map(bl => {
        const key = bl.document_id || bl.reference_id || bl.title;
        const isRef = bl.source_type === 'ref';
        return (
          <div
            key={key}
            role="button"
            tabIndex={0}
            className="doc-item doc-item--right-panel"
            onClick={() => handleBacklinkClick(bl)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.currentTarget.click(); }
            }}
          >
            <span className="w-3" />
            <span className="doc-icon"><FileText size={14} /></span>
            <span className="doc-label">
              {bl.title}{isRef ? ` ${t('refBadge')}` : ''}
            </span>
          </div>
        );
      })}

      {/* ── Outgoing ── */}
      {OUTGOING_SECTIONS.map(({ type, titleKey, icon: Icon }) => {
        const items = outgoingLinks.filter(l => l.type === type);
        if (items.length === 0) return null;
        return (
          <div key={type}>
            <SectionHeader icon={<Icon size={11} />} title={t(titleKey)} />

            {items.map((link, i) => (
              <div
                key={`${link.target}-${i}`}
                role="button"
                tabIndex={0}
                className="doc-item doc-item--right-panel"
                onClick={() => handleOutgoingClick(link)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.currentTarget.click(); }
                }}
              >
                <span className="w-3" />
                <span className="doc-icon"><Icon size={14} /></span>
                <span className="doc-label">{link.label}</span>
              </div>
            ))}
          </div>
        );
      })}
      {!hasOutgoing && (
        <div>
          <SectionHeader icon={<ExternalLink size={11} />} title={t('outgoingLinks')} />
          <p className="text-ui-base text-text-dim text-center py-2 px-2">{t('noOutgoingLinks')}</p>
        </div>
      )}
    </div>
  );
}
