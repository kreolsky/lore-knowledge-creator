/**
 * DocumentTree: accessible tree view for document hierarchy with keyboard navigation.
 *
 * // ARCH: Roving tabindex managed imperatively via DOM — focusedDocId stored in a ref,
 * // tabIndex attributes set directly on DOM elements. Zero React re-renders on focus change.
 * // This scales to hundreds of documents without performance degradation.
 *
 * Store slices: documentTree, collapsedDocIds, currentDocument, currentReference.
 */
// SYSTEM: document-tree — accessible tree view with keyboard nav and hover preview

import type React from 'react';
import { memo, useState, useRef, useCallback, useMemo } from 'react';
import { FileText, Trash2, Plus, ChevronRight, ChevronDown, Pencil, BookOpen, Menu, FolderUp } from 'lucide-react';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { readRefOpenMode, refIsScope } from '../store/ui-store/documents-slice';
import { useShallow } from 'zustand/react/shallow';
import { apiClient } from '../api/client';
import { emit } from '../events';
import { useEvent } from '../hooks/useEvent';
import { ancestorIds } from '../utils/document-ancestors';
import { Button } from './ui';
import { DocumentTreeNode } from '../types';
import { useTranslation } from '../i18n';
import { PREVIEW_WIDTH } from '../utils/preview-geometry';
import { HoverPreviewPopup } from './HoverPreviewPopup';
import { useDocumentPreview } from '../hooks/useDocumentPreview';
import { useDocumentRoute } from '../hooks/useDocumentRoute';
import { useHoverPreview } from '../hooks/useHoverPreview';
import { useTreeDragReorder } from '../hooks/useTreeDragReorder';
import { keyIconClassFromCapabilities } from '../utils/key-icon';
import s from './Sidebar.module.css';

const SHIFT_OFFSET = 40;

const DocumentTreeItem = memo(function DocumentTreeItem({
  doc,
  level = 0,
  onDelete,
  onCreateChild,
  onChangeParent,
  canEdit = true,
  onDocHover,
  onDocHoverLeave,
}: {
  doc: DocumentTreeNode,
  level?: number,
  onDelete: (id: string, name: string) => void,
  onCreateChild: (parentId: string) => void,
  onChangeParent: (doc: DocumentTreeNode, anchorRect: DOMRect) => void,
  canEdit?: boolean,
  onDocHover: (docId: string, titleEl: HTMLElement) => void,
  onDocHoverLeave: (e?: React.MouseEvent) => void,
}) {
  const { t } = useTranslation();
  const { documentId } = useDocumentRoute();
  // INVARIANT: a row NEVER subscribes to the flat `documents` array.
  // Why: the subscription is a reference compare, so every `setDocuments` re-rendered
  // every row and defeated this component's own `memo` — the create/delete flash. The
  // one consumer (handleRenameSubmit) reads the live list at call time instead; rows
  // re-render through the `doc` prop, whose identity patchTree already preserves.
  const { currentReference, currentDocument, setCurrentDocument, setDocuments, referenceSourceDocId, snapshotPreview } = useAppStore(useShallow(s => ({ currentReference: s.currentReference, currentDocument: s.currentDocument, setCurrentDocument: s.setCurrentDocument, setDocuments: s.setDocuments, referenceSourceDocId: s.referenceSourceDocId, snapshotPreview: s.snapshotPreview })));
  const isExpanded = !useUIStore(s => s.collapsedDocIds.includes(doc.document_id));
  const toggleDocExpanded = useUIStore(s => s.toggleDocExpanded);
  const [isRenaming, setIsRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const [gearOpen, setGearOpen] = useState(false);
  const gearEnterTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const renameInputRef = useRef<HTMLInputElement>(null);
  const labelRef = useRef<HTMLDivElement>(null);

  const handleGearEnter = useCallback(() => {
    gearEnterTimer.current = setTimeout(() => setGearOpen(true), 400);
  }, []);

  const handleGearLeave = useCallback(() => {
    if (gearEnterTimer.current) {
      clearTimeout(gearEnterTimer.current);
      gearEnterTimer.current = null;
    }
    setGearOpen(false);
  }, []);

  const children = doc.children;
  const hasChildren = children.length > 0;
  // INVARIANT: highlight keys off the route param when present, else the open
  // doc's id. Why: on /s/:token the URL is the token (no /docs/:id param), so
  // `documentId` is undefined and the selected sub-doc got no plaque. Authed is
  // unaffected — there `documentId === currentDocument.document_id`.
  const activeDocId = documentId ?? currentDocument?.document_id;
  // Panel quick preview: the open reference is NOT the tree's scope — read it
  // through the ONE projection so the current document stays active and no
  // ref-parent/ref-source plaque fires while the ref shows in the Refs tab.
  const refOpenMode = useUIStore(s => readRefOpenMode(s.documents[currentDocument?.document_id ?? '']));
  const scopeReference = refIsScope(refOpenMode) ? currentReference : null;
  const isActive = doc.document_id === activeDocId && !scopeReference;
  const isRefParent = !!scopeReference && doc.document_id === scopeReference.document_id;
  // INVARIANT: a row is never both the reference's source and its parent — the
  // parent plaque (blue) wins. Why: both `active` and `ref-parent` on one row
  // lets the later `.active .doc-item-actions` rule paint a gray plate under the
  // buttons of a blue row (reference opened from its own parent doc).
  const isRefSource = !!scopeReference && !isRefParent && (
    (!!referenceSourceDocId && doc.document_id === referenceSourceDocId) ||
    (!referenceSourceDocId && doc.document_id === activeDocId)
  );
  // INVARIANT: Snapshot-parent highlight hides while a reference is open.
  // Why: reference viewer takes visual precedence over snapshot preview.
  const isSnapshotParent = !!snapshotPreview && !scopeReference && doc.document_id === snapshotPreview.document_id;

  const startRename = () => {
    if (!canEdit) return;
    setRenameValue(doc.title);
    setIsRenaming(true);
    setTimeout(() => renameInputRef.current?.focus(), 50);
  };

  const handleRenameSubmit = async () => {
    const trimmed = renameValue.trim();
    if (!trimmed) { setIsRenaming(false); return; }
    if (trimmed === doc.title) { setIsRenaming(false); return; }
    if (useAppStore.getState().accessLevel !== 'full') return;
    try {
      const updatedDoc = await apiClient.patch(`/documents/${doc.document_id}`, { title: trimmed });
      setDocuments(useAppStore.getState().documents.map(d => d.document_id === doc.document_id ? updatedDoc : d));
      if (doc.document_id === documentId) {
        setCurrentDocument(updatedDoc);
      }
      setIsRenaming(false);
    } catch (err) {
      console.error('Failed to rename document', err);
      useAppStore.getState().showToast(t('renameDocumentFailed'), 'error');
    }
  };

  return (
    <div role="none">
      <div
        ref={labelRef}
        role="treeitem"
        aria-selected={isActive}
        aria-expanded={hasChildren ? isExpanded : undefined}
        tabIndex={-1}
        data-doc-id={doc.document_id}
        data-parent-id={doc.parent_id ?? ''}
        className={`doc-item ${isActive ? 'active' : ''} ${isRefSource ? 'active' : ''} ${isRefParent ? 'ref-parent' : ''} ${isSnapshotParent ? 'snapshot-parent' : ''} ${level > 0 ? 'doc-item-child' : ''}`}
        style={level > 0 ? { paddingLeft: level * 12 + 4 } : undefined}
        onMouseEnter={() => {
          if (labelRef.current) onDocHover(doc.document_id, labelRef.current);
        }}
        onMouseLeave={(e) => onDocHoverLeave(e)}
        onClick={() => {
          if (isRenaming) return;
          const wasViewingSnapshot = !!useAppStore.getState().snapshotPreview;
          if (doc.document_id !== documentId || scopeReference) {
            emit('navigate-to-document', { documentId: doc.document_id, restore: true });
          } else if (wasViewingSnapshot) {
            // WHY: collab manages content in CM6, not in the store — currentDocument.content
            // is stale. Refetch to get actual content for the remounted editor.
            useAppStore.getState().setCurrentDocument(doc);
            apiClient.get(`/documents/${doc.document_id}`).then(data => {
              useAppStore.getState().setCurrentDocument(data);
            });
          }
        }}
      >
        {hasChildren ? (
          // WHY: wide ~30px hitbox (chevron + doc icon) so expand/collapse is easy to hit
          // without aiming at the tiny chevron — left zone toggles, rest of the row navigates.
          <div
            onClick={(e) => { e.stopPropagation(); toggleDocExpanded(doc.document_id); }}
            className="flex items-center shrink-0 cursor-pointer text-text-dim"
            // negative margin + equal padding extends the hitbox left to the row edge
            // WITHOUT changing the indent: clickable area = indent gap + chevron + doc icon.
            // INVARIANT: rowLeftPad must equal the row's actual left padding so the toggle
            // hitbox reaches the true row edge — level>0 sets inline paddingLeft (overrides
            // CSS), level 0 falls back to .doc-item's base `padding: 7px 4px` (4px).  Why: any gap between rowLeftPad and the real row padding falls through to the row onClick (navigate) instead of toggling expand.
            // Why: a leftover gap there fell through to the row onClick (navigate) instead
            // of toggling — a narrow dead strip at the very left edge.
            style={(() => { const rowLeftPad = level > 0 ? level * 12 + 4 : 4; return { marginLeft: -rowLeftPad, paddingLeft: rowLeftPad }; })()}
          >
            {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            <span className={`doc-icon ${keyIconClassFromCapabilities(doc.key_capabilities, doc.public_share)}`}>
              <FileText size={14} />
            </span>
          </div>
        ) : (
          // Mirror the hasChildren wrapper (no inner gap + text-dim) so the doc icon
          // sits at the same indent and same light color regardless of children.
          <div className="flex items-center shrink-0 text-text-dim">
            <span className="w-3" />
            <span className={`doc-icon ${keyIconClassFromCapabilities(doc.key_capabilities, doc.public_share)}`}>
              <FileText size={14} />
            </span>
          </div>
        )}
        {isRenaming ? (
          <input
            ref={renameInputRef}
            className="doc-rename-input"
            data-rename-input
            value={renameValue}
            onChange={e => setRenameValue(e.target.value)}
            onKeyDown={e => {
              e.stopPropagation();
              if (e.key === 'Enter') { e.preventDefault(); handleRenameSubmit(); }
              if (e.key === 'Escape') { setIsRenaming(false); }
            }}
            onBlur={handleRenameSubmit}
            onClick={e => e.stopPropagation()}
          />
        ) : (
          <span
            className="doc-label"
            onDoubleClick={e => { e.stopPropagation(); startRename(); }}
          >
            {doc.title}
          </span>
        )}
        {!isRenaming && canEdit && (
          <div className="doc-item-actions" onClick={e => e.stopPropagation()}>
            <div className={`doc-gear-zone ${gearOpen ? 'doc-gear-open' : ''}`} onMouseEnter={handleGearEnter} onMouseLeave={handleGearLeave}>
              <span className="doc-gear-icon">
                <Menu size={13} />
              </span>
              <div className="doc-gear-actions">
                <button
                  className="doc-action-btn"
                  title={t('rename')}
                  onClick={e => { e.stopPropagation(); startRename(); }}
                >
                  <Pencil size={13} />
                </button>
                <button
                  className="doc-action-btn delete"
                  title={t('delete')}
                  onClick={e => { e.stopPropagation(); onDelete(doc.document_id, doc.title); }}
                >
                  <Trash2 size={13} />
                </button>
                {/* WHY: a system document is unmovable (the server answers 403), so it offers no picker. */}
                {!doc.is_system && (
                  <button
                    className="doc-action-btn"
                    title={t('changeParent')}
                    onClick={e => {
                      e.stopPropagation();
                      onChangeParent(doc, e.currentTarget.getBoundingClientRect());
                    }}
                  >
                    <FolderUp size={13} />
                  </button>
                )}
              </div>
            </div>
            <button
              className="doc-action-btn add-child"
              title={t('addSubDocument')}
              onClick={e => { e.stopPropagation(); onCreateChild(doc.document_id); }}
            >
              <Plus size={13} />
            </button>
          </div>
        )}
      </div>
      {isExpanded && hasChildren && (
        <div role="group">
          {children.map(child => (
            <DocumentTreeItem
              key={child.document_id}
              doc={child}
              level={level + 1}
              onDelete={onDelete}
              onCreateChild={onCreateChild}
              onChangeParent={onChangeParent}
              canEdit={canEdit}
              onDocHover={onDocHover}
              onDocHoverLeave={onDocHoverLeave}
            />
          ))}
        </div>
      )}
    </div>
  );
});

interface DocumentTreeProps {
  onDelete: (id: string, name: string) => void;
  onCreateChild: (parentId: string) => void;
  onChangeParent: (doc: DocumentTreeNode, anchorRect: DOMRect) => void;
  canEdit: boolean;
}

/**
 * Tree container with WAI-ARIA tree pattern and imperative roving tabindex.
 *
 * // INVARIANT: focusedIdRef is the single source of truth for keyboard focus.
 * // It is never read by React render — only by the keyboard handler and DOM mutations.  Why: focus is imperative DOM state, not React state — a ref avoids re-rendering the tree on every focus move while the keyboard handler still reads the live value.
 */
export function DocumentTree({ onDelete, onCreateChild, onChangeParent, canEdit }: DocumentTreeProps) {
  const { t } = useTranslation();
  const { documentId } = useDocumentRoute();
  const { currentProject, documentTree } = useAppStore(useShallow(s => ({ currentProject: s.currentProject, documentTree: s.documentTree })));
  const currentReference = useAppStore(s => s.currentReference);
  const currentDocument = useAppStore(s => s.currentDocument);
  const referenceSourceDocId = useAppStore(s => s.referenceSourceDocId);
  const snapshotPreview = useAppStore(s => s.snapshotPreview);
  // Same projection as DocumentTreeItem: in 'panel' quick preview the reference
  // takes part in nothing but its own tab, so the tree (incl. the project-index
  // row) highlights as if no reference were open.
  const scopeRefOpenMode = useUIStore(s => readRefOpenMode(s.documents[currentDocument?.document_id ?? '']));
  const scopeReference = refIsScope(scopeRefOpenMode) ? currentReference : null;
  const collapsedDocIds = useUIStore(s => s.collapsedDocIds);
  const toggleDocExpanded = useUIStore(s => s.toggleDocExpanded);
  const expandDocs = useUIStore(s => s.expandDocs);
  const setSidebarTab = useUIStore(s => s.setSidebarTab);

  const treeRef = useRef<HTMLDivElement>(null);
  // INVARIANT: focusedIdRef tracks the currently focused doc ID for roving tabindex.
  // Mutated imperatively — never triggers re-renders.  Why: roving tabindex needs the focused id on each keystroke to pick the next focusable row; a ref gives the handler the live value without a re-render per focus move.
  const focusedIdRef = useRef<string | null>(null);

  // INVARIANT(security): drag reordering is gated on edit access (handler no-ops + the rows
  // only respond when canEdit). Never rely on hiding alone.  Why: hiding is not enforcement — a readonly user could still dispatch a drag event, so the handler no-ops and the rows ignore drag unless canEdit (defense in depth).
  useTreeDragReorder(treeRef, canEdit);

  // reveal-in-tree — explicit "show me where this document is" gesture
  // (breadcrumb active crumb). Expands collapsed ancestors, switches the sidebar
  // to the docs tab, then centers the row. Never auto, never collapses. Positioning
  // only — no highlight, and the roving-tabindex focus below is not touched.
  useEvent('reveal-in-tree', useCallback(({ documentId }: { documentId: string }) => {
    // Read the live documents list at event time (not a render subscription) —
    // the handler fires rarely and must see the freshest parent chain.
    expandDocs(ancestorIds(useAppStore.getState().documents, documentId));
    if (useUIStore.getState().sidebarTab !== 'docs') setSidebarTab('docs');
    // Center after React mounts the newly-revealed rows — double rAF lands after
    // one paint. Target by data-doc-id (the memoized item may mount fresh, so a
    // captured ref would be stale). Ancestors not yet loaded → row not found, no-op.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        treeRef.current?.querySelector<HTMLElement>(`[data-doc-id="${documentId}"]`)?.scrollIntoView({ block: 'center' });
      });
    });
  }, [expandDocs, setSidebarTab]));

  const [hoverDocId, setHoverDocId] = useState<string | null>(null);
  const { content: hoverContent, error: hoverError, loading: hoverLoading } = useDocumentPreview(hoverDocId);

  const hover = useHoverPreview({
    shiftOffset: SHIFT_OFFSET,
    getPopupLeft: (rect) => {
      const sidebarEl = document.querySelector('.sidebar') as HTMLElement | null;
      const sidebarRight = sidebarEl ? sidebarEl.getBoundingClientRect().right : rect.right;
      return Math.min(sidebarRight + 16, window.innerWidth - PREVIEW_WIDTH - 12);
    },
  });

  // WHY no isPublicShare gate here (was previously early-return): the public
  // path now works because `fetchDocumentContent` (called transitively via
  // useDocumentPreview) routes through /public/{token}/documents/{id} when a
  // public token is set — no authed 401, no redirect off /s/:token. Hover-
  // preview now has parity with the authed surface. An out-of-scope doc 404s
  // and surfaces as the preview's error state (no silent empty popup).
  const handleDocHover = useCallback((docId: string, titleEl: HTMLElement) => {
    setHoverDocId(docId);
    hover.handleHover(titleEl);
  }, [hover.handleHover]);

  /** Flat list of visible doc IDs in tree order (respects collapsed state). */
  const visibleDocIds = useMemo(() => {
    const ids: string[] = [];
    if (currentProject?.index_doc_id) ids.push(currentProject.index_doc_id);
    function walk(nodes: DocumentTreeNode[]) {
      for (const node of nodes) {
        ids.push(node.document_id);
        if (node.children.length > 0 && !collapsedDocIds.includes(node.document_id)) {
          walk(node.children);
        }
      }
    }
    walk(documentTree);
    return ids;
  }, [documentTree, collapsedDocIds, currentProject?.index_doc_id]);

  /** Find the tree node by ID (for expand/collapse). */
  const findTreeNode = useCallback((id: string): DocumentTreeNode | undefined => {
    function search(nodes: DocumentTreeNode[]): DocumentTreeNode | undefined {
      for (const n of nodes) {
        if (n.document_id === id) return n;
        const found = search(n.children);
        if (found) return found;
      }
    }
    return search(documentTree);
  }, [documentTree]);

  /** Imperatively move roving tabindex to a new doc ID. Zero re-renders. */
  const moveFocus = useCallback((id: string) => {
    const container = treeRef.current;
    if (!container) return;
    // Reset previous
    if (focusedIdRef.current) {
      const prev = container.querySelector<HTMLElement>(`[data-doc-id="${focusedIdRef.current}"]`);
      if (prev) prev.tabIndex = -1;
    }
    // Set new
    const next = container.querySelector<HTMLElement>(`[data-doc-id="${id}"]`);
    if (next) {
      next.tabIndex = 0;
      next.focus();
    }
    focusedIdRef.current = id;
  }, []);

  const handleTreeKeyDown = useCallback((e: React.KeyboardEvent) => {
    const ids = visibleDocIds;
    if (ids.length === 0) return;
    const focused = focusedIdRef.current;
    const currentIdx = focused ? ids.indexOf(focused) : -1;

    switch (e.key) {
      case 'ArrowDown': {
        e.preventDefault();
        const next = currentIdx < ids.length - 1 ? ids[currentIdx + 1] : ids[0];
        moveFocus(next);
        break;
      }
      case 'ArrowUp': {
        e.preventDefault();
        const prev = currentIdx > 0 ? ids[currentIdx - 1] : ids[ids.length - 1];
        moveFocus(prev);
        break;
      }
      case 'ArrowRight': {
        e.preventDefault();
        if (!focused) break;
        const node = findTreeNode(focused);
        if (node && node.children.length > 0 && collapsedDocIds.includes(focused)) {
          toggleDocExpanded(focused);
        } else if (node && node.children.length > 0) {
          moveFocus(node.children[0].document_id);
        }
        break;
      }
      case 'ArrowLeft': {
        e.preventDefault();
        if (!focused) break;
        const node = findTreeNode(focused);
        if (node && node.children.length > 0 && !collapsedDocIds.includes(focused)) {
          toggleDocExpanded(focused);
        }
        break;
      }
      case 'Enter':
      case ' ': {
        e.preventDefault();
        if (focused) {
          emit('navigate-to-document', { documentId: focused, restore: true });
        }
        break;
      }
    }
  }, [visibleDocIds, collapsedDocIds, toggleDocExpanded, findTreeNode, moveFocus]);

  return (
    <>
      <div
        ref={treeRef}
        role="tree"
        tabIndex={0}
        aria-label={t('documentTree')}
        className={s.sidebarDocs}
        onKeyDown={handleTreeKeyDown}
      >
        {currentProject?.index_doc_id && (() => {
          // See DocumentTreeItem INVARIANT: fall back to the open doc's id when the
          // route param is absent (/s/:token has no /docs/:id param).  Why: public-share routes carry no /docs/:id param, so without the fallback the tree couldn't identify the open doc and would highlight no row.
          const activeDocId = documentId ?? currentDocument?.document_id;
          return (
          <div
            role="treeitem"
            aria-selected={activeDocId === currentProject.index_doc_id && !scopeReference}
            tabIndex={-1}
            data-doc-id={currentProject.index_doc_id}
            className={`doc-item doc-item-context ${
              (activeDocId === currentProject.index_doc_id && !scopeReference) ? 'active' : ''
            } ${
              !!scopeReference && currentProject.index_doc_id !== scopeReference.document_id && (
                (!!referenceSourceDocId && currentProject.index_doc_id === referenceSourceDocId) ||
                (!referenceSourceDocId && currentProject.index_doc_id === activeDocId)
              ) ? 'active' : ''
            } ${
              !!scopeReference && currentProject.index_doc_id === scopeReference?.document_id ? 'ref-parent' : ''
            } ${
              !!snapshotPreview && !scopeReference && currentProject.index_doc_id === snapshotPreview.document_id ? 'snapshot-parent' : ''
            }`}
            onClick={() => emit('navigate-to-document', { documentId: currentProject.index_doc_id!, restore: true })}
          >
            <span className="doc-icon">
              <BookOpen size={14} />
            </span>
            <span className="doc-label">{t('projectContext')}</span>
          </div>
          );
        })()}
        {documentTree.map((doc) => (
          <DocumentTreeItem
            key={doc.document_id}
            doc={doc}
            onDelete={onDelete}
            onCreateChild={onCreateChild}
            onChangeParent={onChangeParent}
            canEdit={canEdit}
            onDocHover={handleDocHover}
            onDocHoverLeave={hover.handleHoverLeave}
          />
        ))}

        {canEdit && (
          <div className="sticky bottom-0 bg-surface mt-0.5">
            <Button variant="dashed" onClick={() => emit('open-create-doc', {})}>
              <Plus size={14} />
              {t('addDocument')}
            </Button>
          </div>
        )}
      </div>
      <HoverPreviewPopup
        hover={hover}
        content={hoverContent}
        error={hoverError}
        loading={hoverLoading}
      />
    </>
  );
}
