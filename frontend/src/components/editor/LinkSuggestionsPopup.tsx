/**
 * Floating popup for inserting document/reference/note/URL links.
 *
 * Triggered by Cmd+K hotkey via 'show-link-suggestions' custom event.
 * Supports mode switching (docs / ref: / note: / http://) via Cmd+Arrow.
 * Rendered via createPortal to document.body.
 */

import { useState, useEffect, useLayoutEffect, useRef, useMemo, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { EditorView } from '@codemirror/view';
import { undo } from '@codemirror/commands';
import { useAppStore } from '../../store/app-store';
import { useNoteChatStore } from '../../store/note-chat-store';
import { useEvent } from '../../hooks/useEvent';
import { useNavMode } from '../../hooks/useNavMode';
import { useDocumentPreview } from '../../hooks/useDocumentPreview';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { useReferencePreview } from '../../hooks/useReferencePreview';
import { usePopupSlot } from '../../hooks/usePopupSlot';
import { useEditorView } from '../../editor/active-editor';
import { apiClient } from '../../api/client';
import { Reference } from '../../types';
import { FieldInput } from '../ui';
import { PickerPreviewPopup } from '../PickerPreviewPopup';
import { computeBesideBoxPreviewPosition } from '../../utils/popup-position';
import { PREVIEW_PICKER_MAX_HEIGHT } from '../../utils/preview-geometry';
import { useTranslation } from '../../i18n';
import { buildParentMap, sortDocumentsForChat, sortReferences } from '../../utils/document-sort';
import { referenceFileUrl } from '../../utils/reference-url';

const POPUP_WIDTH = 340;

export function LinkSuggestionsPopup() {
  const { t } = useTranslation();
  const getEditorView = useEditorView();
  const noteChatSessions = useNoteChatStore(s => s.sessions);
  const documents = useAppStore(s => s.documents);
  const currentDocument = useAppStore(s => s.currentDocument);
  const currentProject = useAppStore(s => s.currentProject);

  const [isOpen, setIsOpen] = useState(false);
  // INVARIANT: the link-creation popup offers EVERY reference in the project, not just the
  // current doc's ancestor-scoped set (which the shared store.references slice / ReferencesPanel
  // hold). Why: a reference attached to a sibling document must be linkable. Fetched project-wide
  // on open; the store slice stays ancestor-scoped for the panel's depth-sorted display.
  const [projectReferences, setProjectReferences] = useState<Reference[]>([]);
  const [insertPos, setInsertPos] = useState<number | null>(null);
  const [coords, setCoords] = useState<{ top: number; lineTop: number; left: number } | null>(null);
  // Final position decided post-measure (popup height varies by tab/results); flips above the
  // anchored line when there's no room below. null until the first layout pass.
  const [layout, setLayout] = useState<{ top: number; left: number } | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);

  const [previewDocId, setPreviewDocId] = useState<string | null>(null);
  const [previewRefId, setPreviewRefId] = useState<string | null>(null);
  const { content: rawContent, error: docError, loading: docLoading } = useDocumentPreview(previewDocId);
  const previewDocTitle = useDocumentTitle(previewDocId);
  const lastContentRef = useRef('');
  if (rawContent !== undefined) lastContentRef.current = rawContent;
  const previewContent = lastContentRef.current;

  const targetViewRef = useRef<EditorView | null>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const previewPanelRef = useRef<HTMLDivElement>(null);
  const previewPanelRootCallback = useCallback((el: HTMLDivElement | null) => {
    previewPanelRef.current = el;
  }, []);
  const listRef = useRef<HTMLDivElement>(null);
  const insertedRef = useRef(false);
  const { isKeyboard, activateKeyboard } = useNavMode();

  // Mode derived from query
  const isExternalMode = /^https?:\/\//i.test(searchQuery);
  const isRefMode = !isExternalMode && /^ref/i.test(searchQuery);
  const isNoteMode = !isExternalMode && !isRefMode && /^note/i.test(searchQuery);
  const refSearch = isRefMode ? searchQuery.replace(/^ref\s*:?\s*/i, '') : '';
  const noteSearch = isNoteMode ? searchQuery.replace(/^note\s*:?\s*/i, '') : '';
  const isDocMode = !isExternalMode && !isRefMode && !isNoteMode;

  type Mode = 'doc' | 'ref' | 'note' | 'url';
  const currentMode: Mode = isExternalMode ? 'url' : isRefMode ? 'ref' : isNoteMode ? 'note' : 'doc';
  const setMode = (mode: Mode) => {
    const userText = searchQuery.replace(/^(ref\s*:?\s*|note\s*:?\s*|https?:\/\/)/i, '');
    const prefix = { doc: '', ref: 'ref: ', note: 'note: ', url: 'http://' }[mode];
    setSearchQuery(prefix + userText);
    setSelectedIndex(0);
    setPreviewDocId(null);
    setPreviewRefId(null);
  };

  const anchorlessNotes = noteChatSessions.filter(s => s.is_note && s.anchor_offset_start == null);

  const parentMap = useMemo(() => buildParentMap(documents), [documents]);

  const linkItems = useMemo(() => {
    if (isExternalMode) return [{ id: searchQuery, label: t('insertExternalLink'), prefix: '' }];
    if (isRefMode) return sortReferences(
      projectReferences.filter(r => r.title.toLowerCase().includes(refSearch.toLowerCase())),
      refSearch,
    ).map(r => ({ id: `ref:${r.reference_id}`, label: r.title, prefix: 'ref' }));
    if (isNoteMode) return anchorlessNotes
      .filter(s => (s.title ?? '').toLowerCase().includes(noteSearch.toLowerCase()))
      .map(s => {
        const label = s.title ?? '';
        return { id: `note:${s.session_id}`, label: label.slice(0, 60) + (label.length > 60 ? '\u2026' : ''), prefix: 'note' };
      });
    return sortDocumentsForChat(
      documents.filter(d => !d.is_index).filter(d => d.title.toLowerCase().includes(searchQuery.toLowerCase())),
      currentDocument?.document_id ?? null,
      parentMap,
      searchQuery,
    ).map(d => ({ id: d.document_id, label: d.title, prefix: '' }));
  }, [isExternalMode, isRefMode, isNoteMode, searchQuery, refSearch, noteSearch, projectReferences, anchorlessNotes, documents, currentDocument, parentMap, t]);

  // ARCH: Preview sync uses firstItemId (primitive) instead of linkItems (reference) in deps,
  // because t() from useTranslation is unstable — linkItems gets a new reference every render,
  // which would override arrow/hover preview selections.
  const firstItemId = linkItems.length > 0 ? linkItems[0].id : null;

  useEffect(() => {
    if (!isOpen) return;
    if (isDocMode && firstItemId) setPreviewDocId(firstItemId);
    else if (isRefMode && firstItemId) setPreviewRefId(firstItemId.replace(/^ref:/, ''));
  }, [firstItemId, isOpen, isDocMode, isRefMode]);

  // Fetch all project references when the popup opens (ref: mode lists them).
  useEffect(() => {
    if (!isOpen || !currentProject) return;
    let cancelled = false;
    apiClient.get(`/references?project_id=${currentProject.project_id}&limit=1000`)
      .then((refs: Reference[]) => { if (!cancelled) setProjectReferences(refs); })
      .catch(() => {
        if (!cancelled) useAppStore.getState().showToast(t('failedToLoadReferences'), 'error');
      });
    return () => { cancelled = true; };
  }, [isOpen, currentProject?.project_id]);

  const clampedIndex = Math.max(0, Math.min(selectedIndex, linkItems.length - 1));

  // On close: undo link formatting if no link was inserted, reset preview
  useEffect(() => {
    if (isOpen) return;
    setPreviewDocId(null);
    setPreviewRefId(null);
    // Guard: targetViewRef is null on mount, so undo won't fire on init
    if (!insertedRef.current && targetViewRef.current) {
      undo(targetViewRef.current);
      targetViewRef.current.focus();
    }
  }, [isOpen]);

  // Listen for show-link-suggestions event
  useEvent('show-link-suggestions', useCallback((detail: { pos: number; coords: { top: number; lineTop: number; left: number } | null; editorView: EditorView }) => {
    setInsertPos(detail.pos);
    setCoords(detail.coords);
    setLayout(null);
    setSearchQuery('');
    setSelectedIndex(0);
    setIsOpen(true);
    insertedRef.current = false;
    targetViewRef.current = detail.editorView ?? null;
  }, [documents]));

  // Click-outside dismissal
  useEffect(() => {
    if (!isOpen) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (popupRef.current && !popupRef.current.contains(target)
          && (!previewPanelRef.current || !previewPanelRef.current.contains(target))) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [isOpen]);

  // Floating-popup slot: gating the render below yields to a higher-priority popup.
  // Don't force-close on suppression — that would fire during the one-render acquire lag.
  const isActive = usePopupSlot('link-suggestions', isOpen);

  // Post-measure placement: flip above the line when there's no room below (popup height
  // varies by tab/results, so measured after render), then clamp into the viewport.
  // WHY: depends on isActive — the popup mounts one render after isOpen (slot-acquire
  // lag), so popupRef is null on the first pass; without isActive the effect never re-runs
  // and the flip/clamp is skipped, leaving the popup off-screen at the bottom.  Why: popupRef is null on the first render (slot-acquire lag); isActive in deps re-runs the effect once mounted, else the flip/clamp is skipped and the popup sits off-screen.
  useLayoutEffect(() => {
    const el = popupRef.current;
    if (!isOpen || !isActive || !coords || !el) return;
    const h = el.offsetHeight;

    let top = coords.top + 4;
    const spaceBelow = window.innerHeight - coords.top;
    if (spaceBelow < h + 8 && coords.lineTop - h - 4 >= 8) {
      top = coords.lineTop - h - 4;
    }
    top = Math.max(8, Math.min(top, window.innerHeight - h - 8));

    const left = Math.min(coords.left, window.innerWidth - POPUP_WIDTH - 8);

    setLayout(prev => (prev && prev.top === top && prev.left === left ? prev : { top, left }));
  }, [isOpen, isActive, coords, currentMode, linkItems.length]);

  const handleInsertLink = (targetId: string) => {
    insertedRef.current = true;
    const targetView = targetViewRef.current ?? getEditorView();
    if (insertPos !== null && targetView) {
      targetView.dispatch({
        changes: { from: insertPos, to: insertPos, insert: targetId },
        selection: { anchor: insertPos + targetId.length + 1 },
      });
      targetView.focus();
    }
    setIsOpen(false);
  };

  // Set previewDocId directly from keyboard navigation
  const handleArrow = (dir: 1 | -1) => {
    activateKeyboard();
    setSelectedIndex(i => {
      const next = dir === 1 ? Math.min(i + 1, linkItems.length - 1) : Math.max(i - 1, 0);
      if (isDocMode && linkItems[next]) setPreviewDocId(linkItems[next].id);
      if (isRefMode && linkItems[next]) setPreviewRefId(linkItems[next].id.replace(/^ref:/, ''));
      return next;
    });
  };

  // Reference preview fetch MUST run before the early return (rules-of-hooks). The
  // list is metadata-only, so the body is lazy-fetched; image refs + hydrated text
  // refs need no fetch. File refs never fetch — a downloadable binary has no body.
  const previewRefForHook = previewRefId ? projectReferences.find(r => r.reference_id === previewRefId) ?? null : null;
  const previewRefFetchId = previewRefForHook && previewRefForHook.media_type !== 'image'
    && previewRefForHook.media_type !== 'file'
    && previewRefForHook.content === undefined ? previewRefId : null;
  const { preview: previewRefFetched, error: refError, loading: refLoading } = useReferencePreview(previewRefFetchId);

  if (!isOpen || !coords || !isActive) return null;

  // Reference preview: image refs use file_path; hydrated text refs use their store content.
  const previewRef = previewRefId ? projectReferences.find(r => r.reference_id === previewRefId) ?? null : null;
  const isPreviewImage = previewRef?.media_type === 'image';
  const previewRefContent = isPreviewImage ? '' : (previewRef?.content ?? previewRefFetched?.content ?? '');
  const previewRefImageUrl = isPreviewImage && previewRef?.file_path
    ? referenceFileUrl(previewRef.reference_id, previewRef.file_path)
    : null;

  // Effective position: measured layout once available, else the below-anchor default.
  const popupTop = layout?.top ?? coords.top + 4;
  const popupLeftPos = layout?.left ?? coords.left;

  const showPreview = isDocMode || isRefMode;
  const previewPos = showPreview
    ? computeBesideBoxPreviewPosition(popupLeftPos, popupTop, POPUP_WIDTH, PREVIEW_PICKER_MAX_HEIGHT)
    : null;

  return createPortal(
    <>
      <div
        ref={popupRef}
        className="link-suggest-popup"
        style={{ top: popupTop, left: popupLeftPos }}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex border-b border-border mb-1.5">
          {([
            { id: 'doc', label: t('documents') },
            { id: 'ref', label: t('references') },
            { id: 'note', label: t('notes') },
            { id: 'url', label: t('url') },
          ] as { id: Mode; label: string }[]).map(tab => (
            <div
              key={tab.id}
              role="tab"
              tabIndex={-1}
              className={`flex-1 text-ui-sm py-1 text-center border-b-2 -mb-px cursor-pointer ${currentMode === tab.id ? 'border-[var(--accent)] text-text font-medium' : 'border-transparent text-text-muted hover:text-text'}`}
              onMouseDown={(e) => { e.preventDefault(); setMode(tab.id); }}
            >
              {tab.label}
            </div>
          ))}
        </div>
        <div className="mb-1.5">
          <FieldInput
            autoFocus
            type="text"
            placeholder={t('linkSuggestionsPlaceholder')}
            value={searchQuery}
            onChange={(e) => { setSearchQuery(e.target.value); setSelectedIndex(0); }}
            className="w-full text-ui-base py-1.5 px-2.5"
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                setIsOpen(false);
                return;
              }
              if (e.key === 'ArrowDown') {
                e.preventDefault();
                handleArrow(1);
                return;
              }
              if (e.key === 'ArrowUp') {
                e.preventDefault();
                handleArrow(-1);
                return;
              }
              if ((e.metaKey || e.ctrlKey) && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) {
                e.preventDefault();
                const modes: Mode[] = ['doc', 'ref', 'note', 'url'];
                const dir = e.key === 'ArrowRight' ? 1 : -1;
                const idx = modes.indexOf(currentMode);
                setMode(modes[(idx + dir + modes.length) % modes.length]);
                return;
              }
              if (e.key === 'Enter') {
                e.preventDefault();
                if (linkItems.length > 0) handleInsertLink(linkItems[clampedIndex].id);
              }
            }}
          />
        </div>
        <div ref={listRef} className={`list-scroll overflow-y-auto flex-1${isKeyboard ? ' nav-keyboard' : ''}`}>
          {linkItems.map((item, i) => (
            <button
              key={item.id}
              onClick={() => handleInsertLink(item.id)}
              className={`link-suggest-item${i === clampedIndex ? ' selected' : ''}`}
              // INVARIANT: scroll-to-active only on keyboard nav, never on mouse hover/edge.
              // Why: hovering up from the trigger button scrolled the list and hid the first
              // pre-selected item; auto-scroll must require an explicit user action.
              ref={el => { if (i === clampedIndex && isKeyboard) el?.scrollIntoView({ block: 'nearest' }); }}
              onMouseEnter={() => {
                if (isKeyboard) return;
                if (isDocMode) setPreviewDocId(item.id);
                if (isRefMode) setPreviewRefId(item.id.replace(/^ref:/, ''));
              }}
            >
              {item.prefix && <span className="opacity-50 text-ui-xs mr-1.5">{item.prefix}</span>}
              {item.label}
            </button>
          ))}
          {linkItems.length === 0 && (
            <div className="link-suggest-empty">
              {isRefMode ? t('noReferencesHint') : isNoteMode ? t('noNotesFound') : t('noDocumentsFound')}
            </div>
          )}
        </div>
      </div>
      <PickerPreviewPopup
        pos={previewPos}
        title={isRefMode ? previewRef?.title : previewDocTitle}
        content={isRefMode && previewRefImageUrl ? undefined : (isRefMode ? previewRefContent : previewContent)}
        imageUrl={isRefMode ? (previewRefImageUrl ?? undefined) : undefined}
        error={isRefMode ? refError : docError}
        loading={isRefMode ? refLoading : docLoading}
        popupRef={previewPanelRootCallback}
      />
    </>,
    document.body
  );
}
