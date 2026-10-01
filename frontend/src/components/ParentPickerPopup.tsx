/**
 * Floating popup for changing a document's parent.
 *
 * Mirrors LinkSuggestionsPopup layout (search + list + preview) but only shows
 * documents. Excludes the target document and all its descendants to prevent cycles,
 * and the Memory folder with its cards (never a destination, on every call site).
 * Rendered via createPortal to document.body.
 */

import { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { useAppStore } from '../store/app-store';
import { apiClient } from '../api/client';
import { serverRefusalDetail } from '../utils/server-refusal-detail';
import { FieldInput } from './ui';
import { useTranslation } from '../i18n';
import { buildParentMap, sortDocumentsForChat } from '../utils/document-sort';
import { useNavMode } from '../hooks/useNavMode';
import { useDocumentPreview } from '../hooks/useDocumentPreview';
import { usePopupSlot } from '../hooks/usePopupSlot';
import { PickerPreviewPopup } from './PickerPreviewPopup';
import { computeBesideBoxPreviewPosition } from '../utils/popup-position';
import { PREVIEW_PICKER_MAX_HEIGHT } from '../utils/preview-geometry';
import type { Document, DocumentTreeNode } from '../types';

const POPUP_WIDTH = 340;

function getDescendantIds(docId: string, documents: Document[]): Set<string> {
  const childrenMap = new Map<string, string[]>();
  for (const d of documents) {
    if (d.parent_id) {
      const siblings = childrenMap.get(d.parent_id) ?? [];
      siblings.push(d.document_id);
      childrenMap.set(d.parent_id, siblings);
    }
  }
  const result = new Set<string>();
  const stack = [docId];
  while (stack.length) {
    const id = stack.pop()!;
    for (const child of childrenMap.get(id) ?? []) {
      result.add(child);
      stack.push(child);
    }
  }
  return result;
}

/** The Memory folder and every card under it. */
function getMemoryIds(documents: Document[]): Set<string> {
  const folder = documents.find(d => d.system_role === 'memory_folder');
  if (!folder) return new Set<string>();
  return new Set([folder.document_id, ...getDescendantIds(folder.document_id, documents)]);
}

interface Props {
  /** Document reparent mode */
  doc?: DocumentTreeNode;
  /** Reference reparent mode — current document_id of the reference */
  currentDocumentId?: string | null;
  /** Callback for reference mode — receives new document_id (null = project-level) */
  onMoved?: (newDocumentId: string | null) => void;
  anchorRect: DOMRect;
  onClose: () => void;
  /** Open above anchor instead of below (default: false) */
  above?: boolean;
  /** Hide preview panel (default: false) */
  hidePreview?: boolean;
  /**
   * Override the document list (default: the app store's documents). Used by
   * cross-project surfaces (AccessPanel move section) that feed the picker a
   * TARGET project's tree instead of the current project's.
   */
  documents?: Document[];
  /** Override the "no parent" sentinel label (default: mode-dependent t() key). */
  noParentLabel?: string;
}

export function ParentPickerPopup({ doc, currentDocumentId, onMoved, anchorRect, onClose, above, hidePreview, documents: documentsProp, noParentLabel: noParentLabelProp }: Props) {
  const { t } = useTranslation();
  // Mounted === wants open. Dominates hover previews so opening this hides them (item 1).
  const isActive = usePopupSlot('parent-picker', true);
  const isRefMode = !doc;
  // Hook called unconditionally (rules-of-hooks); the prop merely overrides it.
  const storeDocuments = useAppStore(s => s.documents);
  const documents = documentsProp ?? storeDocuments;
  const setDocuments = useAppStore(s => s.setDocuments);
  const showToast = useAppStore(s => s.showToast);

  const [searchQuery, setSearchQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);

  const [previewDocId, setPreviewDocId] = useState<string | null>(null);
  const { content: rawContent, error: previewError, loading: previewLoading } = useDocumentPreview(previewDocId);
  const lastContentRef = useRef('');
  if (rawContent !== undefined) lastContentRef.current = rawContent;
  const previewContent = lastContentRef.current;
  const { isKeyboard, activateKeyboard } = useNavMode();

  const popupRef = useRef<HTMLDivElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  const previewRootCallback = useCallback((el: HTMLDivElement | null) => {
    previewRef.current = el;
  }, []);

  // INVARIANT: the Memory folder and its cards are never offered as a parent.
  // Why: a document filed there silently becomes memory the agent retrieves; the
  // server refuses the move from outside memory (documents/move.py).
  const excludedIds = useMemo(
    () => new Set([
      ...(doc ? getDescendantIds(doc.document_id, documents) : []),
      ...getMemoryIds(documents),
    ]),
    [doc, documents],
  );

  // ARCH: "No parent" sentinel uses empty string — backend treats "" as clearing parent_id
  const NO_PARENT_ID = '';

  const currentParentId = doc ? (doc.parent_id ?? '') : (currentDocumentId ?? '');
  const noParentLabel = noParentLabelProp
    ?? (isRefMode ? t('projectLevelAllDocs') : t('noParentTopLevel'));

  const parentMap = useMemo(() => buildParentMap(documents), [documents]);

  const items = useMemo(() => {
    const q = searchQuery.toLowerCase();
    const currentDocId = doc?.document_id ?? currentDocumentId ?? null;
    const filtered = documents.filter(d =>
      !d.is_index
      && (isRefMode || d.document_id !== doc?.document_id)
      && !excludedIds.has(d.document_id)
      && d.title.toLowerCase().includes(q),
    );
    const sorted = sortDocumentsForChat(filtered, currentDocId, parentMap, searchQuery);
    const docs = sorted.map(d => ({ id: d.document_id, label: d.title }));

    const noParentItem = { id: NO_PARENT_ID, label: noParentLabel };
    const showNoParent = !q || noParentItem.label.toLowerCase().includes(q);
    return showNoParent ? [noParentItem, ...docs] : docs;
  }, [searchQuery, documents, doc?.document_id, currentDocumentId, excludedIds, isRefMode, noParentLabel, parentMap]);

  useEffect(() => {
    if (items.length > 0 && items[0].id) {
      setPreviewDocId(items[0].id);
    } else {
      setPreviewDocId(null);
    }
  }, [items]);

  const clampedIndex = Math.max(0, Math.min(selectedIndex, items.length - 1));

  // Click-outside dismissal
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (popupRef.current && !popupRef.current.contains(target)
          && (!previewRef.current || !previewRef.current.contains(target))) {
        onClose();
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [onClose]);

  const handleSelect = async (parentId: string) => {
    if (isRefMode) {
      onMoved?.(parentId || null);
      onClose();
      return;
    }
    // Warn when a reparent would move the doc (and its descendants) OUT of a
    // scoped agent key's subtree — those keys silently lose access to the moved
    // branch. Moving a subtree ROOT itself is fine (descendants follow, the
    // subtree stays internally consistent). Scope roots are the caller's own
    // docs carrying the agent capability in the server-enriched documents
    // payload (plan "glimmering-knitting-pebble") — no fetch, no silent
    // degradation path.
    const roots = documents
      .filter(d => d.key_capabilities?.includes('agent'))
      .map(d => d.document_id);
    if (roots.length) {
      const movedId = doc!.document_id;
      // The new parent's subtree membership is what decides the crossing.
      const crossing = roots.filter(rootId => {
        const subtree = new Set([rootId, ...getDescendantIds(rootId, documents)]);
        return subtree.has(movedId) && !subtree.has(parentId);
      });
      if (crossing.length) {
        const ok = window.confirm(t('agentKeyReparentWarn', { count: crossing.length }));
        if (!ok) { onClose(); return; }
      }
    }
    try {
      const updatedDoc = await apiClient.patch(`/documents/${doc!.document_id}`, {
        parent_id: parentId,
      });
      const docs = useAppStore.getState().documents;
      setDocuments(docs.map(d =>
        d.document_id === doc!.document_id ? updatedDoc : d,
      ));
    } catch (err) {
      // WHY: the popup stays open on failure so the user can pick another parent;
      // the server's reason (cycle, Memory) names why this one was refused.
      showToast(serverRefusalDetail(err) ?? t('changeParentFailed'), 'error');
      return;
    }
    onClose();
  };

  const handleArrow = (dir: 1 | -1) => {
    activateKeyboard();
    setSelectedIndex(i => {
      const next = dir === 1 ? Math.min(i + 1, items.length - 1) : Math.max(i - 1, 0);
      const item = items[next];
      if (item && item.id) setPreviewDocId(item.id);
      else setPreviewDocId(null);
      return next;
    });
  };

  // Position popup below or above the anchor, clamped to viewport
  const popupLeft = Math.max(4, Math.min(anchorRect.left, window.innerWidth - POPUP_WIDTH - 12));
  const spaceBelow = window.innerHeight - anchorRect.bottom - 8;
  const spaceAbove = anchorRect.top - 8;
  // Sized to fit ~9 document rows (no tab header). Keep in sync with the inline maxHeight below.
  const POPUP_ESTIMATED_HEIGHT = 330;
  const openAbove = above ?? (spaceBelow < POPUP_ESTIMATED_HEIGHT && spaceAbove > spaceBelow);
  const popupTop = openAbove
    ? Math.max(4, anchorRect.top - POPUP_ESTIMATED_HEIGHT)
    : anchorRect.bottom + 4;

  // Preview panel on whichever side has more space
  const currentItem = items[clampedIndex];
  const previewPos = (!hidePreview && currentItem && currentItem.id)
    ? computeBesideBoxPreviewPosition(popupLeft, popupTop, POPUP_WIDTH, PREVIEW_PICKER_MAX_HEIGHT)
    : null;

  // Suppressed by a higher-priority popup — render nothing (see usePopupSlot contract).
  if (!isActive) return null;

  return createPortal(
    <>
      <div
        ref={popupRef}
        className="link-suggest-popup"
        style={{ top: popupTop, left: popupLeft, maxHeight: POPUP_ESTIMATED_HEIGHT }}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="mb-1.5">
          <FieldInput
            autoFocus
            type="text"
            placeholder={t('searchDocuments')}
            value={searchQuery}
            onChange={(e) => { setSearchQuery(e.target.value); setSelectedIndex(0); }}
            className="w-full text-ui-base py-1.5 px-2.5"
            onKeyDown={(e) => {
              if (e.key === 'Escape') { onClose(); return; }
              if (e.key === 'ArrowDown') { e.preventDefault(); handleArrow(1); return; }
              if (e.key === 'ArrowUp') { e.preventDefault(); handleArrow(-1); return; }
              if (e.key === 'Enter') {
                e.preventDefault();
                if (items.length > 0) handleSelect(items[clampedIndex].id);
              }
            }}
          />
        </div>
        <div className={`list-scroll overflow-y-auto flex-1${isKeyboard ? ' nav-keyboard' : ''}`}>
          {items.map((item, i) => (
            <button
              key={item.id || '__no_parent__'}
              onClick={() => handleSelect(item.id)}
              className={`link-suggest-item${i === clampedIndex ? ' selected' : ''}${item.id === currentParentId ? ' current-parent' : ''}`}
              // INVARIANT: scroll-to-active only on keyboard nav, never on mouse hover/edge.
              // Why: hovering up from the trigger button scrolled the list and hid the first
              // pre-selected item; auto-scroll must require an explicit user action.
              ref={el => { if (i === clampedIndex && isKeyboard) el?.scrollIntoView({ block: 'nearest' }); }}
              onMouseEnter={() => {
                if (isKeyboard) return;
                setSelectedIndex(i);
                if (item.id) setPreviewDocId(item.id);
                else setPreviewDocId(null);
              }}
            >
              {!item.id && <span className="opacity-50 text-ui-xs mr-1.5">↑</span>}
              {item.label}
              {item.id === currentParentId && <span className="ml-auto text-ui-2xs px-1.5 py-0.5 bg-[var(--sticky-yellow-light)] text-text-muted">{t('current')}</span>}
            </button>
          ))}
          {items.length === 0 && (
            <div className="link-suggest-empty">{t('noDocumentsFound')}</div>
          )}
        </div>
      </div>
      {previewDocId && (
        <PickerPreviewPopup
          pos={previewPos}
          // WHY not useDocumentTitle: `documents` may be another project's tree (the
          // AccessPanel move target), absent from the store the hook reads.
          title={documents.find(d => d.document_id === previewDocId)?.title}
          content={previewContent}
          error={previewError}
          loading={previewLoading}
          popupRef={previewRootCallback}
        />
      )}
    </>,
    document.body,
  );
}
