/** Application header with breadcrumb navigation, document rename, recording indicator. */
// ARCH: Extracted from App.tsx — always-mounted header with PinLock hotkey.
// Document navigation's owner is navigation/open-document.ts (SYSTEM:
// document-navigation), mounted once by ProjectShell — not this component.
// PUBLIC SHARE: when `isPublicShare` (from ui-store), the breadcrumb renders the project
// name (from ui-store.publicProjectName, disclosed by /tree) + document title —
// but no ancestors and no rename (those route into /projects/… or fire authed
// PATCH), and the project crumb is non-interactive. The logo links to `/` (landing), not
// `/projects`.

import { useEffect, useMemo } from 'react';
import { withOptimistic } from '../utils/optimistic';
import { useLocation, Link } from 'react-router-dom';
import { useAppStore } from '../store/app-store';
import { useDocState, useUIStore } from '../store/ui-store';
import { readRefOpenMode, refIsScope } from '../store/ui-store/documents-slice';
import { useShallow } from 'zustand/react/shallow';
import { apiClient } from '../api/client';
import { Mic, Play, Square } from 'lucide-react';
import { isEditorShell } from '../utils/routing';
import { Breadcrumb } from './Breadcrumb';
import { useTranslation } from '../i18n';
import { useElapsedTime } from '../hooks/useElapsedTime';
import { AdminVersion } from './AdminVersion';
import { emit } from '../events';
import { formatDuration } from './references/ref-utils';

export function Header() {
  const location = useLocation();
  const isCabinet = location.pathname === '/cabinet';
  const isAdmin = location.pathname === '/admin';
  const isProjectList = location.pathname === '/projects';
  const isPublicShare = useUIStore(s => s.isPublicShare);
  const publicProjectName = useUIStore(s => s.publicProjectName);
  const { currentProject, currentDocument, rawReference, currentTable, currentTableLabel, documents } = useAppStore(useShallow(s => ({ currentProject: s.currentProject, currentDocument: s.currentDocument, rawReference: s.currentReference, currentTable: s.currentTable, currentTableLabel: s.currentTableLabel, documents: s.documents })));
  // Panel quick preview: the header is the DOCUMENT's — title, rename and the
  // share-link button follow the doc scope; the previewed ref's own chrome lives
  // on the Refs tab plaque. Reads through the ONE projection.
  const refOpenMode = useUIStore(s => readRefOpenMode(s.documents[currentDocument?.document_id ?? ''], s.compactLayout));
  const currentReference = refIsScope(refOpenMode) ? rawReference : null;
  const accessLevel = useAppStore(s => s.accessLevel);
  const sectionCrumb = useAppStore(s => s.sectionCrumb);
  const rightPanelOpen = useDocState(currentDocument?.document_id ?? null).rightPanelOpen;
  // Compact viewport: no floating right tab bar over the header, so nothing to reserve.
  const compactLayout = useUIStore(s => s.compactLayout);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.metaKey && e.shiftKey && e.key.toLowerCase() === 'l') {
        const user = useAppStore.getState().currentUser;
        if (user?.has_pin) {
          e.preventDefault();
          useAppStore.getState().setPinLocked(true);
        }
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  // Navigate-to-document is owned by navigation/useDocumentNavigation (mounted
  // once in ProjectShell) — this component no longer holds a handler.

  const { t } = useTranslation();
  const recording = useAppStore(s => s.recording);
  const recordingElapsed = useElapsedTime(recording?.startedAt ?? null);
  const activeItem = currentReference || currentDocument;
  // A focused table shows its (live) label in the document-title leaf of the breadcrumb
  // (like a reference), with the document pushed into the ancestor chain. The label is read
  // reactively from the store so a rename (local OR peer) updates it. Plan: tables-as-references-badges.
  const activeTitle = (currentTable ? currentTableLabel : null) ?? activeItem?.title ?? null;

  const ancestors = useMemo(() => {
    if (!currentDocument) return [];
    const chain: { id: string; title: string }[] = [];
    let parentId = currentDocument.parent_id;
    while (parentId) {
      const parent = documents.find(d => d.document_id === parentId);
      if (!parent) break;
      chain.unshift({ id: parent.document_id, title: parent.title });
      parentId = parent.parent_id;
    }
    if (currentReference || currentTable) {
      chain.push({ id: currentDocument.document_id, title: currentDocument.title });
    }
    return chain;
  }, [currentDocument, currentReference, currentTable, documents]);

  const handleDocumentRename = async (newTitle: string) => {
    if (!activeItem || !newTitle.trim() || newTitle === activeItem.title) return;

    if (currentReference) {
      await withOptimistic(
        { ...currentReference, title: newTitle },
        currentReference,
        (r) => useAppStore.getState().setCurrentReference(r),
        () => apiClient.patch(`/references/${currentReference.reference_id}`, { title: newTitle }),
      );
    } else if (currentDocument) {
      const setDoc = (d: typeof currentDocument) => {
        useAppStore.getState().setCurrentDocument(d);
        useAppStore.getState().setDocuments(documents.map(x => x.document_id === d.document_id ? d : x));
      };
      await withOptimistic(
        { ...currentDocument, title: newTitle },
        currentDocument,
        setDoc,
        () => apiClient.patch(`/documents/${currentDocument.document_id}`, { title: newTitle }),
      );
    }
  };

  return (
    <header className="flex items-center gap-0 h-12 bg-header-bg border-b border-border-soft pl-4 shrink-0 relative z-[13]">
      <Link
        to={isPublicShare ? '/' : '/projects'}
        className="flex items-center no-underline text-text mr-2"
      >
        <div className="w-[26px] h-[26px] bg-accent flex items-center justify-center text-sm text-white tracking-[-0.5px] font-bold">L</div>
      </Link>

      {/*
        PUBLIC: render breadcrumb with the project name (disclosed by /tree) +
        document title. No ancestors (routing into /projects/… would 401), no
        rename (authed PATCH), and the project crumb is a non-interactive no-op
        (no owner-side navigation). The document title is non-interactive.
      */}
      {/* The breadcrumb is for editor shells only — /cabinet and /admin show a
          static label below (their currentProject is a leftover, not context). */}
      {currentProject && !isPublicShare && !isCabinet && !isAdmin && (
        <>
          <div className="w-px h-5 bg-border mx-1" />
          <Breadcrumb
            projectName={currentProject.name}
            ancestors={ancestors}
            documentTitle={activeTitle || null}
            isReference={!!currentReference}
            accessLevel={accessLevel}
            onProjectClick={() => emit('open-sidebar-docs')}
            onAncestorClick={(docId) => {
              emit('navigate-to-document', { documentId: docId });
            }}
            onDocumentRename={handleDocumentRename}
            // Reveal target is always the open document: for a focused reference/table
            // the breadcrumb already pushes the doc into the ancestor chain, so its id
            // is the document the row lives under — one rule, no branch on entity type.
            onDocumentReveal={() => currentDocument && emit('reveal-in-tree', { documentId: currentDocument.document_id })}
          />
        </>
      )}
      {/* Non-editor surfaces: the page title sits in the header like a project crumb. */}
      {(isProjectList || isCabinet || isAdmin) && (
        <>
          <div className="w-px h-5 bg-border mx-1" />
          <span className="text-[15.6px] text-text-muted px-1 py-0.5 select-none">
            {isProjectList ? t('projects') : isCabinet ? t('profile') : t('adminPanel')}
            {isAdmin && <AdminVersion />}
          </span>
          {/* The admin page's active section, as a non-interactive leaf crumb —
              the same `Project : Document` shape as the editor breadcrumb. */}
          {isAdmin && sectionCrumb && (
            <>
              <span className="text-text select-none">:</span>
              <span className="text-[15.6px] text-text px-1 py-0.5 select-none">{sectionCrumb}</span>
            </>
          )}
        </>
      )}
      {isPublicShare && currentDocument && (
        <>
          <div className="w-px h-5 bg-border mx-1" />
          <Breadcrumb
            projectName={publicProjectName ?? ''}
            ancestors={[]}
            documentTitle={currentDocument.title || null}
            isReference={false}
            accessLevel="readonly"
            /* No-ops: public viewer has no project navigation / no rename. */
            onProjectClick={() => {}}
            onAncestorClick={() => {}}
            onDocumentRename={async () => {}}
          />
        </>
      )}

      <div className="ml-auto flex items-center gap-1" style={!rightPanelOpen && !compactLayout ? { marginRight: 'var(--right-tab-bar-w)' } : undefined}>
        {/* Recording button: hidden on public share (gated by accessLevel + URL check). */}
        {!isPublicShare && isEditorShell(location.pathname) && (
          <button
            className={`header-recording-btn ${recording ? '' : 'header-recording-btn--idle'}`}
            onClick={() => recording ? emit('stop-recording') : emit('start-recording')}
            disabled={!recording && (!currentDocument || accessLevel !== 'full')}
            title={recording ? t('recordingFor', { title: recording.targetDocumentTitle }) : accessLevel !== 'full' ? t('recordingRequiresAccess') : currentDocument ? t('recordAudioFor', { title: currentDocument.title }) : t('openDocToRecord')}
          >
            <Mic size={13} />
            <span className="recording-label">{recording ? formatDuration(recordingElapsed) : 'rec'}</span>
            <span className="recording-icon-slot">
              <Square size={11} className={`recording-icon--stop ${recording ? 'visible' : 'invisible'}`} />
              <Play size={11} className="recording-icon--play" />
            </span>
          </button>
        )}

      </div>
    </header>
  );
}
