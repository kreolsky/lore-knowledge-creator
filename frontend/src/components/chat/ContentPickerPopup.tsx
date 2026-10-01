/**
 * Multi-select document/reference picker for chat context injection.
 *
 * Rendered via createPortal to document.body. Reuses document-sort ranking.
 * Click toggles selection without closing; closes only on Esc or click-outside.
 * Documents / References segmented tab at top, Cmd+Arrow to cycle.
 * Preview panel reuses LinkSuggestionsPopup architecture (lazy-fetch + cache).
 */

import { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { Check, Link2 } from 'lucide-react';
import { useAppStore } from '../../store/app-store';
import { apiClient } from '../../api/client';
import { useTranslation } from '../../i18n';
import { FieldInput } from '../ui';
import { buildParentMap, sortDocumentsForChat, sortReferencesByProximity } from '../../utils/document-sort';
import { referenceFileUrl } from '../../utils/reference-url';
import { useNavMode } from '../../hooks/useNavMode';
import { useDocumentPreview } from '../../hooks/useDocumentPreview';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { useReferencePreview } from '../../hooks/useReferencePreview';
import { usePopupSlot } from '../../hooks/usePopupSlot';
import { PickerPreviewPopup } from '../PickerPreviewPopup';
import { computeBesideBoxPreviewPosition } from '../../utils/popup-position';
import { PREVIEW_MAX_HEIGHT } from '../../utils/preview-geometry';
import { addItemToContext, removeItemFromContext, addGhostDelta, removeGhostDelta, GHOST_SESSION_ID } from '../../chat/context';
import { computeClaimed } from '../../utils/cascade-selection';
import { fetchDocumentLinks, fetchReferenceLinks, linkCache } from '../../api/links';
import type { Document, Reference } from '../../types';

const POPUP_WIDTH = 340;
const POPUP_ESTIMATED_HEIGHT = 380;
// ARCH: the References tab
// lists ALL project references (cross-doc included), fetched on open. The
// /references endpoint hard-caps at limit ≤ 1000 with no server title filter;
// the picker filters client-side. When the response hits the cap the list is
// truncated to the newest N (server order = created_at DESC) — surfaced via a
// non-blocking notice (no-silent-degradation: an absent ref must not look like
// "no such ref").
const REFS_LIMIT = 1000;

type Tab = 'documents' | 'references';

interface Props {
  sessionId: string;
  selectedDocIds: string[];
  selectedRefIds: string[];
  onClose: (reason?: 'esc') => void;
  anchorRect: DOMRect;
  above?: boolean;
  triggerRef?: { current: HTMLDivElement | null };
  initialTab?: Tab;
  anchorDocId: string | null;
  sessionRefId: string | null;
}

export function ContentPickerPopup({ sessionId, selectedDocIds, selectedRefIds, onClose, anchorRect, above, triggerRef, initialTab, anchorDocId, sessionRefId }: Props) {
  const { t } = useTranslation();
  const isActive = usePopupSlot('content-picker', true);
  const documents = useAppStore(s => s.documents);
  const references = useAppStore(s => s.references);
  const projectId = useAppStore(s => s.currentProject?.project_id);

  // B.1: all project references (cross-doc included), fetched on open. Empty
  // until the fetch resolves; on failure it falls back to the store `references`
  // (open-doc scope) + a toast. `refsTruncated` flags a response that hit the
  // 1000-ref cap (non-blocking notice).
  const [projectReferences, setProjectReferences] = useState<Reference[]>([]);
  const [refsTruncated, setRefsTruncated] = useState(false);
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    apiClient.get(`/references?project_id=${projectId}&limit=${REFS_LIMIT}`)
      .then((refs: Reference[]) => {
        if (cancelled) return;
        setProjectReferences(refs);
        setRefsTruncated(refs.length >= REFS_LIMIT);
      })
      .catch(() => {
        if (cancelled) return;
        // No-silent-degradation: surface the failure, then fall back to the
        // store's open-doc-scope list so the tab stays usable.
        useAppStore.getState().showToast(t('failedToLoadReferences'), 'error');
        setProjectReferences(useAppStore.getState().references);
        setRefsTruncated(false);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- one-shot fetch on open; the popup (un)mounts on close. t/references deliberately read fresh.
  }, [projectId]);

  const [activeTab, setActiveTab] = useState<Tab>(initialTab ?? 'documents');
  const [searchQuery, setSearchQuery] = useState('');
  const [hoveredIndex, setHoveredIndex] = useState(-1);
  const popupRef = useRef<HTMLDivElement>(null);
  const previewPanelRef = useRef<HTMLDivElement>(null);
  const previewPanelRootCallback = useCallback((el: HTMLDivElement | null) => {
    previewPanelRef.current = el;
  }, []);

  const [previewDocId, setPreviewDocId] = useState<string | null>(null);
  const [previewRefId, setPreviewRefId] = useState<string | null>(null);
  const { content: rawContent, error: docError, loading: docLoading } = useDocumentPreview(previewDocId);
  const previewDocTitle = useDocumentTitle(previewDocId);
  const lastContentRef = useRef('');
  if (rawContent !== undefined) lastContentRef.current = rawContent;
  const previewContent = lastContentRef.current;
  const { isKeyboard, activateKeyboard } = useNavMode();

  // INVARIANT: These freeze the selection at the moment the picker opens, used
  // ONLY for stable visual sort order ("pre-selected first").  Why: the frozen IDs are cosmetic (pre-selected-first sort); the real cascade add/subtract lives in chat/context.ts, so these never affect behavior — only sort order. They have no role
  // in cascade behavior — link fetching and add/subtract live in chat/context.ts.
  // Newly (un)selected items don't reshuffle while the picker stays open.
  const [frozenSelectedDocIds] = useState(() => new Set(selectedDocIds));
  const [frozenSelectedRefIds] = useState(() => new Set(selectedRefIds));

  // parentMap over the FULL project document tree — shared by both tabs so
  // reference proximity is measured against the real document hierarchy (a
  // cross-doc ref's owning doc may live anywhere in the tree).
  const parentMap = useMemo(() => buildParentMap(documents), [documents]);

  // B.3: effective anchor for reference proximity. The prop (anchorDocId) carries
  // the open document (with session fallback resolved upstream in ChatInput).
  // When the prop is null (no open doc, no session), fall back to looking up
  // sessionRefId's owning doc via the project reference list (the store
  // `references` is open-doc-scope only).
  const effectiveAnchorDocId = useMemo(() => {
    if (anchorDocId) return anchorDocId;
    if (sessionRefId) {
      const source = projectReferences.length ? projectReferences : references;
      return source.find(r => r.reference_id === sessionRefId)?.document_id ?? null;
    }
    return null;
  }, [anchorDocId, sessionRefId, projectReferences, references]);

  const docItems = useMemo(() => {
    const q = searchQuery.toLowerCase();
    const filtered = documents.filter(d =>
      !d.is_index && d.title.toLowerCase().includes(q),
    );
    const sorted = sortDocumentsForChat(filtered, anchorDocId, parentMap, searchQuery);
    return [
      ...sorted.filter(d => frozenSelectedDocIds.has(d.document_id)),
      ...sorted.filter(d => !frozenSelectedDocIds.has(d.document_id)),
    ];
  }, [searchQuery, documents, anchorDocId, parentMap, frozenSelectedDocIds]);

  // B.1/B.3: list ALL project references (projectReferences), proximity-sorted.
  // Selected-first lives INSIDE the comparator (tier 1, keyed by the frozen
  // selection set) so it does not reshuffle while the picker is open — the old
  // post-sort filter(has)/filter(!has) split is dropped for refs. (Documents tab
  // keeps its existing split — unchanged.)
  const refItems = useMemo(() => {
    const q = searchQuery.toLowerCase();
    const source = projectReferences.length ? projectReferences : references;
    const filtered = source.filter(r => r.title.toLowerCase().includes(q));
    return sortReferencesByProximity(
      filtered,
      effectiveAnchorDocId,
      sessionRefId,
      frozenSelectedRefIds,
      parentMap,
      searchQuery,
    );
  }, [searchQuery, projectReferences, references, effectiveAnchorDocId, sessionRefId, frozenSelectedRefIds, parentMap]);

  const items = activeTab === 'documents' ? docItems : refItems;

  // Warm the shared link cache for currently-selected items so the Link2 icon
  // can show "claimed by another selected parent" hints. Read-only — never
  // feeds back into selection logic. After each fetch we bump cacheVersion so
  // `claimed` below recomputes against the now-fresher cache.
  const [cacheVersion, setCacheVersion] = useState(0);
  useEffect(() => {
    const warm = async (kind: 'doc' | 'ref', id: string) => {
      const key = `${kind}:${id}`;
      if (linkCache.has(key)) return;
      const fetcher = kind === 'doc' ? fetchDocumentLinks : fetchReferenceLinks;
      try {
        await fetcher(id);
        setCacheVersion(v => v + 1);
      } catch { /* indicator-only; failure is non-critical */ }
    };
    selectedDocIds.forEach(id => warm('doc', id));
    selectedRefIds.forEach(id => warm('ref', id));
  }, [selectedDocIds, selectedRefIds]);

  // INVARIANT: claimed MUST recompute whenever linkCache mutates. cacheVersion
  // in the deps captures warm-fetch completion; without it the memo froze the
  // result computed at mount time against a partially-warm cache.  Why: the claimed memo depends on linkCache; without cacheVersion in deps it froze at mount and missed late-arriving links.
  const claimed = useMemo(() => {
    const keys = [
      ...selectedDocIds.map(d => `doc:${d}`),
      ...selectedRefIds.map(r => `ref:${r}`),
    ];
    return computeClaimed(keys, linkCache);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- cacheVersion intentionally invalidates the memo when linkCache mutates
  }, [selectedDocIds, selectedRefIds, cacheVersion]);

  const isClaimed = (kind: 'doc' | 'ref', id: string) =>
    kind === 'doc' ? claimed.docs.has(id) : claimed.refs.has(id);

  // WHY: Every checkbox click writes to the chat context. There is no local
  // "draft" state, no opt-out memory, no "parent vs child" heuristic — the toggle
  // simply chooses add vs remove based on whether the id is currently selected.  Why: there is no draft/opt-out state — the picker is a direct view of the chat context, so a click immediately adds/removes in context (single source of truth).
  // A GHOST session has no stored bucket: writes fold manual DELTAS into the derived
  // value (addGhostDelta/removeGhostDelta). A materialized session writes through
  // the cascade pipeline (addItemToContext/removeItemFromContext). The cascade
  // (merge/subtract first circle) is folded by deriveGhostContext on the ghost side,
  // and by applyCascade on the materialized side.
  const toggleItem = (id: string) => {
    const kind: 'doc' | 'ref' = activeTab === 'documents' ? 'doc' : 'ref';
    const isSelected = kind === 'doc'
      ? selectedDocIds.includes(id)
      : selectedRefIds.includes(id);
    if (sessionId === GHOST_SESSION_ID) {
      if (isSelected) removeGhostDelta(kind, id);
      else addGhostDelta(kind, id);
    } else if (isSelected) {
      removeItemFromContext(sessionId, kind, id);
    } else {
      addItemToContext(sessionId, kind, id);
    }
  };

  const isSelected = (id: string) => {
    return activeTab === 'documents'
      ? selectedDocIds.includes(id)
      : selectedRefIds.includes(id);
  };

  const toggleItemRef = useRef(toggleItem);
  toggleItemRef.current = toggleItem;

  const updatePreview = (index: number) => {
    const item = items[index];
    if (!item) return;
    if (activeTab === 'documents') setPreviewDocId((item as Document).document_id);
    if (activeTab === 'references') setPreviewRefId((item as Reference).reference_id);
  };

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (popupRef.current && !popupRef.current.contains(target)
          && (!previewPanelRef.current || !previewPanelRef.current.contains(target))
          && (!triggerRef?.current || !triggerRef.current.contains(target))) {
        onClose();
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [onClose, triggerRef]);

  useEffect(() => {
    if (items.length > 0) {
      setHoveredIndex(0);
      const first = items[0];
      if (activeTab === 'documents') setPreviewDocId((first as Document).document_id);
      if (activeTab === 'references') setPreviewRefId((first as Reference).reference_id);
    } else {
      setHoveredIndex(-1);
      setPreviewDocId(null);
      setPreviewRefId(null);
    }
  }, [searchQuery, activeTab]);

  const popupLeft = Math.max(4, Math.min(anchorRect.left, window.innerWidth - POPUP_WIDTH - 12));
  const spaceBelow = window.innerHeight - anchorRect.bottom - 8;
  const spaceAbove = anchorRect.top - 8;
  const openAbove = above ?? (spaceBelow < POPUP_ESTIMATED_HEIGHT && spaceAbove > spaceBelow);
  const popupTop = openAbove
    ? Math.max(4, anchorRect.top - POPUP_ESTIMATED_HEIGHT)
    : anchorRect.bottom + 4;

  // Preview lookup prefers the full project reference list (so a cross-doc ref
  // resolves metadata for the lazy body fetch); falls back to the store list
  // during the brief pre-fetch window or after a fetch failure. The project-scope
  // LIST is metadata-only, so a cross-doc ref has content === undefined → the
  // existing useReferencePreview lazy GET path fires unchanged (no eager body fetch).
  const previewRef = (() => {
    if (!previewRefId) return null;
    return projectReferences.find(r => r.reference_id === previewRefId)
      ?? references.find(r => r.reference_id === previewRefId)
      ?? null;
  })();
  const isPreviewImage = previewRef?.media_type === 'image';
  // Lazy-fetch the preview body (the list is metadata-only). Image refs resolve from
  // file_path; already-hydrated text refs use their store content.
  const previewRefFetchId = previewRef && !isPreviewImage && previewRef.content === undefined ? previewRefId : null;
  const { preview: previewRefFetched, error: refError, loading: refLoading } = useReferencePreview(previewRefFetchId);
  const previewRefContent = isPreviewImage ? '' : (previewRef?.content ?? previewRefFetched?.content ?? '');
  const previewRefImageUrl = isPreviewImage && previewRef?.file_path
    ? referenceFileUrl(previewRef.reference_id, previewRef.file_path)
    : null;

  const showPreview = activeTab === 'documents' || activeTab === 'references';
  const previewPos = (showPreview && (previewDocId || previewRefId))
    ? computeBesideBoxPreviewPosition(popupLeft, popupTop, POPUP_WIDTH, PREVIEW_MAX_HEIGHT, 12)
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
        <div className="flex border-b border-border mb-1.5">
          <div
            role="tab"
            tabIndex={0}
            className={`flex-1 text-ui-sm py-1 text-center border-b-2 -mb-px cursor-pointer ${activeTab === 'documents' ? 'border-[var(--accent)] text-text font-medium' : 'border-transparent text-text-muted hover:text-text'}`}
            onClick={() => setActiveTab('documents')}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setActiveTab('documents'); } }}
          >
            {t('documents')}
          </div>
          <div
            role="tab"
            tabIndex={0}
            className={`flex-1 text-ui-sm py-1 text-center border-b-2 -mb-px cursor-pointer ${activeTab === 'references' ? 'border-[var(--accent)] text-text font-medium' : 'border-transparent text-text-muted hover:text-text'}`}
            onClick={() => setActiveTab('references')}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setActiveTab('references'); } }}
          >
            {t('references')}
          </div>
        </div>
        <div className="mb-1.5">
          <FieldInput
            autoFocus
            type="text"
            placeholder={activeTab === 'documents' ? t('searchDocuments') : t('searchReferencesPlaceholder')}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                e.preventDefault();
                onClose('esc');
                return;
              }
              if ((e.metaKey || e.ctrlKey) && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) {
                e.preventDefault();
                const tabs: Tab[] = ['documents', 'references'];
                const currentIdx = tabs.indexOf(activeTab);
                const dir = e.key === 'ArrowRight' ? 1 : -1;
                setActiveTab(tabs[(currentIdx + dir + tabs.length) % tabs.length]);
                return;
              }
              if (e.key === 'ArrowDown') {
                e.preventDefault();
                activateKeyboard();
                setHoveredIndex(i => {
                  const next = Math.min(i + 1, items.length - 1);
                  updatePreview(next);
                  return next;
                });
                return;
              }
              if (e.key === 'ArrowUp') {
                e.preventDefault();
                activateKeyboard();
                setHoveredIndex(i => {
                  const next = Math.max(i - 1, 0);
                  updatePreview(next);
                  return next;
                });
                return;
              }
              if (e.key === 'Enter') {
                e.preventDefault();
                if (hoveredIndex >= 0 && hoveredIndex < items.length) {
                  const item = items[hoveredIndex];
                  const id = activeTab === 'documents' ? (item as Document).document_id : (item as Reference).reference_id;
                  toggleItemRef.current(id);
                }
              }
            }}
            className="w-full text-ui-base py-1.5 px-2.5"
          />
        </div>
        <div className={`list-scroll overflow-y-auto flex-1${isKeyboard ? ' nav-keyboard' : ''}`}>
          {activeTab === 'references' && refsTruncated && (
            <div className="px-2 py-1 text-ui-xs text-amber border-b border-amber/30 bg-amber/5">
              {t('referencesListTruncated', { count: projectReferences.length })}
            </div>
          )}
          {items.map((item, i) => {
            const id = activeTab === 'documents' ? (item as Document).document_id : (item as Reference).reference_id;
            const title = activeTab === 'documents' ? (item as Document).title : (item as Reference).title;
            return (
              <div
                key={id}
                role="button"
                tabIndex={0}
                onMouseDown={e => e.preventDefault()}
                onClick={() => toggleItem(id)}
                onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleItem(id); } }}
                onMouseEnter={() => { if (isKeyboard) return; setHoveredIndex(i); updatePreview(i); }}
                // INVARIANT: scroll-to-active only on keyboard nav, never on mouse hover/edge.
                // Why: hovering up from the trigger button scrolled the list and hid the first
                // pre-selected item; auto-scroll must require an explicit user action.
                ref={el => { if (i === hoveredIndex && isKeyboard) el?.scrollIntoView({ block: 'nearest' }); }}
                className={`link-suggest-item gap-2${i === hoveredIndex ? ' selected' : ''}`}
              >
                <span
                  className="shrink-0 w-4 h-4 border border-border flex items-center justify-center"
                  style={isSelected(id) ? { background: 'var(--accent)', borderColor: 'var(--accent)' } : {}}
                >
                  {isSelected(id) && <Check size={12} className="text-white" />}
                </span>
                <span className="truncate flex-1">{title}</span>
                {isClaimed(activeTab === 'documents' ? 'doc' : 'ref', id) && (
                  <Link2 size={10} className="shrink-0 text-text-muted" />
                )}
              </div>
            );
          })}
          {items.length === 0 && (
            <div className="link-suggest-empty">
              {activeTab === 'documents' ? t('noDocumentsFound') : t('noReferencesFound')}
            </div>
          )}
        </div>
      </div>
      <PickerPreviewPopup
        pos={previewPos}
        title={activeTab === 'documents' ? previewDocTitle : previewRef?.title}
        content={activeTab === 'documents' ? previewContent : (previewRefImageUrl ? undefined : previewRefContent)}
        imageUrl={activeTab === 'references' ? (previewRefImageUrl ?? undefined) : undefined}
        error={activeTab === 'documents' ? docError : refError}
        loading={activeTab === 'documents' ? docLoading : refLoading}
        popupRef={previewPanelRootCallback}
      />
    </>,
    document.body,
  );
}
