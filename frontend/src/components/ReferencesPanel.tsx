/**
 * References panel: manages markdown, audio, and image attachments for a project/document.
 *
 * Rendering + CRUD for reference items. Upload, drag-drop, and transcription polling
 * are handled by useReferenceUpload hook.
 *
 * Store slices: references, currentProject, currentDocument.
 */

import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { Plus, Upload, ArrowLeft, Bot, Columns2, Eye, Archive } from 'lucide-react';
import { useAppStore } from '../store/app-store';
import { useChatStore } from '../store/chat-store';
import { claimAutoOpen, openInboxObject } from '../store/inbox-store';
import { useUIStore, useDocState } from '../store/ui-store';
import { readRefOpenMode, type RefOpenMode } from '../store/ui-store/documents-slice';
import { Editor } from './Editor';
import { ReferenceMediaBar } from './editor/ReferenceMediaBar';
import { apiClient } from '../api/client';
import { loadReferences } from '../api/references-fetch';
import { publicReferencesByDoc } from '../api/public-share';
import { useEditorView } from '../editor/active-editor';
import { emit } from '../events';
import { withOptimistic } from '../utils/optimistic';
import { referenceFileUrl, getPublicFileContext } from '../utils/reference-url';
import { publicPanelReferences } from '../utils/public-ref-scope';
import { Reference } from '../types';
import { createMarkdownReference } from './references/create-markdown-reference';
import { ACTIVE_TOGGLE_CLS } from './references/ref-utils';
import TranscriptionAgentConfigManager from './transcription/TranscriptionAgentConfigManager';
import { Button, IconButton, Modal, PanelLoading, PillList } from './ui';
import { useTranslation } from '../i18n';
import { RefCard } from './references/RefCard';
import { refDragAdapter } from './references/refDragAdapter';
import { useSiblingDragReorder } from '../hooks/useSiblingDragReorder';
import { TableBadge } from './references/TableBadge';
import { ImageGallery } from './references/ImageGallery';
import { RefPanelPlaque } from './references/RefPanelPlaque';
import { ParentPickerPopup } from './ParentPickerPopup';
import { useReferenceUpload } from '../hooks/useReferenceUpload';
import { useReferenceDelete } from '../hooks/useReferenceDelete';
import { useReferencePreview } from '../hooks/useReferencePreview';
import { useDocumentTables } from '../hooks/useDocumentTables';
import { useTableBadgeOps } from '../hooks/useTableBadgeOps';
import { HoverPreviewPopup } from './HoverPreviewPopup';
import { useRightPanelHoverPreview } from '../hooks/useRightPanelHoverPreview';
import { type DocumentTableEntry } from './editor/live-preview/table-block-model';

export function ReferencesPanel() {
  const {
    currentProject, currentDocument, currentReference, currentTable, documents, references, accessLevel,
    setCurrentReference, setCurrentTable, setCurrentTableLabel, setReferences, removeReference, updateReference,
    referencesReloadKey,
  } = useAppStore(useShallow(s => ({
    currentProject: s.currentProject,
    currentDocument: s.currentDocument,
    currentReference: s.currentReference,
    currentTable: s.currentTable,
    documents: s.documents,
    references: s.references,
    accessLevel: s.accessLevel,
    setCurrentReference: s.setCurrentReference,
    setCurrentTable: s.setCurrentTable,
    setCurrentTableLabel: s.setCurrentTableLabel,
    setReferences: s.setReferences,
    removeReference: s.removeReference,
    updateReference: s.updateReference,
    referencesReloadKey: s.referencesReloadKey,
  })));
  const isPublicShare = useUIStore(s => s.isPublicShare);
  // PUBLIC: token lives at the URL — read via the module-level context (set by
  // PublicSharePage on mount). When null (authed surface), the authed path runs.
  const publicToken = isPublicShare ? getPublicFileContext() : null;
  const canEdit = !isPublicShare && accessLevel === 'full';
  const { t } = useTranslation();
  const getView = useEditorView();

  const {
    isDragging,
    fileInputRef,
    handleFileInput,
    dragHandlers,
  } = useReferenceUpload();

  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  // Table-badge rename state (SEPARATE from the reference renamingId above —
  // decoupled ids so a table rename never interferes with a reference rename in
  // flight).
  const [tableRenamingId, setTableRenamingId] = useState<string | null>(null);
  const [tableRenameValue, setTableRenameValue] = useState('');
  const [retryRef, setRetryRef] = useState<Reference | null>(null);
  const [refParentPicker, setRefParentPicker] = useState<{ reference: Reference; anchorRect: DOMRect } | null>(null);
  // showAgentConfig toggles the transcription-agent pipeline-config sub-view
  // (the API-key manager moved to the unified Access tab). hasAgentConfig gates
  // the per-card "Send to agent" action; agentRunningRef tracks the in-flight run.
  const [showAgentConfig, setShowAgentConfig] = useState(false);
  const [hasAgentConfig, setHasAgentConfig] = useState(false);
  const [agentRunningRef, setAgentRunningRef] = useState<string | null>(null);
  // INVARIANT: distinguishes "scope still loading" from "scope has no references", so
  // the drag-and-drop empty state never flashes before the /references fetch lands.
  // Why: references.length===0 is also true mid-fetch — gate the empty state on this.
  const [refsLoading, setRefsLoading] = useState(false);

  const [hoverRefId, setHoverRefId] = useState<string | null>(null);
  const hoverRef = hoverRefId ? references.find(r => r.reference_id === hoverRefId) ?? null : null;
  // PUBLIC: the lazy body fetch (useReferencePreview) hits the authed /references/{id}
  // endpoint and would 401+toast on the anonymous surface. Disable hover-preview
  // entirely; the publicReferencesByDoc response carries ref content where present.
  // Lazy-fetch the body for text refs (the list is metadata-only). Image refs resolve
  // from file_path in the list; already-hydrated text refs use their store content.
  // File refs are skipped too — a downloadable binary has no body to fetch OR show
  // (the popup itself is suppressed for them in handleRefHover).
  const hoverFetchId = !isPublicShare && hoverRef && hoverRef.media_type !== 'image' && hoverRef.media_type !== 'file' && hoverRef.content === undefined
    ? hoverRefId : null;
  const { preview: hoverFetched, error: hoverError, loading: hoverLoading } = useReferencePreview(hoverFetchId);
  const { hoverContent, hoverImageUrl } = useMemo(() => {
    if (!hoverRef) return { hoverContent: undefined, hoverImageUrl: undefined };
    if (hoverRef.media_type === 'image' && hoverRef.file_path)
      return { hoverContent: undefined, hoverImageUrl: referenceFileUrl(hoverRef.reference_id, hoverRef.file_path) };
    if (hoverRef.content !== undefined) return { hoverContent: hoverRef.content, hoverImageUrl: undefined };
    return { hoverContent: hoverFetched?.content, hoverImageUrl: undefined };
  }, [hoverRef, hoverFetched]);

  const hover = useRightPanelHoverPreview();

  const handleRefHover = useCallback((refId: string, cardEl: HTMLElement) => {
    // File refs (agent-shared archives) open no hover popup: the card already
    // shows title + size, and the popup body would render a misleading
    // "no content" — the file's bytes are its content (download card in the viewer).
    const ref = useAppStore.getState().references.find(r => r.reference_id === refId);
    if (ref?.media_type === 'file') return;
    setHoverRefId(refId);
    hover.handleHover(cardEl);
  }, [hover.handleHover]);

  const setRefOpenMode = useUIStore(s => s.setRefOpenMode);
  const pinRightPanel = useUIStore(s => s.pinRightPanel);
  // Global "Show archived" filter (READ surface — available to
  // every role incl. readonly/commentator/public-share). OFF → default LIST (hidden);
  // ON → include_archived=true, archived refs sink to the bottom.
  const showArchived = useUIStore(s => s.showArchived);
  const setShowArchived = useUIStore(s => s.setShowArchived);
  // Client-side count of archived refs in the loaded list (only meaningful when showArchived
  // is ON — the OFF fetch excludes them, so the count is always 0 then). Cheapest source;
  // honors the LIST-size INVARIANT (no new count endpoint).
  const archivedCount = useMemo(() => references.filter(r => r.archived === true).length, [references]);

  const prevDocIdRef = useRef(currentDocument?.document_id ?? null);
  const currentDocId = currentDocument?.document_id ?? null;
  const docUIState = useDocState(currentDocId);
  // Project-wide setting (ProjectSettingsPanel), shared by every member; default gallery.
  const previewMode = currentProject?.ref_image_preview ?? true;
  const compactLayout = useUIStore(s => s.compactLayout);
  const refOpenMode = readRefOpenMode(docUIState, compactLayout);
  // INVARIANT: the active toggle shows the CHOSEN mode, whether or not a reference is
  // open right now. Why: the mode decides how the NEXT click on a reference opens it,
  // so the user must see it before clicking — a highlight gated on an open reference
  // told them nothing in advance.
  const modeActive = (mode: RefOpenMode) => refOpenMode === mode;
  // Each toggle sets its own mode, or goes back to 'center' when it is already the mode.
  const toggleMode = (mode: RefOpenMode) => {
    if (currentDocId) setRefOpenMode(currentDocId, refOpenMode === mode ? 'center' : mode);
  };
  // 'panel' mode with an open reference: the Refs tab shows the reference itself instead
  // of the list. The open reference is app-store `currentReference`, not panel-local
  // state, so leaving for Chat and coming back lands on the same open reference.
  const panelRef = refOpenMode === 'panel' ? currentReference : null;
  const isFading = currentDocId !== prevDocIdRef.current && references.length > 0;

  // WHY: this scoped fetch is the ONLY place the panel re-scopes to the current doc.
  // Between loads, references moved/created into other docs persist here (with a parent
  // label) — deliberate stale-until-reload behavior, see useReferenceEvents.  Why: re-scoping only on this fetch gives stable stale-until-reload behavior; live moves/creates persist with a parent label until the next reload. Do not
  // "fix" it by filtering the store list against the current ancestor scope.
  useEffect(() => {
    setShowAgentConfig(false);
    setHasAgentConfig(false);
    if (!currentDocument || (!currentProject && !isPublicShare)) {
      setReferences([]);
      setRefsLoading(false);
      prevDocIdRef.current = currentDocId;
      return;
    }

    let cancelled = false;
    setRefsLoading(true);

    // PUBLIC: use the document-keyed anonymous endpoint (plan "public-document-
    // ids"). The public root doc id is set by PublicSharePage; it gates the
    // public branch — the doc's OWN id is the path segment.
    const loadPromise = isPublicShare && publicToken
      ? publicReferencesByDoc(currentDocument.document_id)
      : loadReferences(currentProject!.project_id, currentDocument.document_id, (cached) => {
          // SWR pre-gate paint: show the last-seen list instantly, then the revalidate
          // fetch below reconciles. NOT a terminal — refsLoading stays true until it lands.
          if (!cancelled) useAppStore.getState().mergeReferences(cached);
        }, showArchived);

    if (isPublicShare) {
      // publicReferencesByDoc returns a flat array — assign directly (no SWR cache).
      Promise.resolve(loadPromise).then((refs) => {
        if (!cancelled) {
          setReferences(refs);
          prevDocIdRef.current = currentDocId;
        }
      }).catch((err) => {
        if (!cancelled) {
          console.error('Failed to load public references:', err);
          useAppStore.getState().showToast(t('failedToLoadReferences'), 'error');
        }
      }).finally(() => {
        if (!cancelled) setRefsLoading(false);
      });
    } else {
      // AUTHED: SWR-cached fetch with merge semantics.
      (loadPromise as Promise<Reference[]>).then((refs) => {
        if (!cancelled) {
          useAppStore.getState().mergeReferences(refs);
          prevDocIdRef.current = currentDocId;
        }
      }).catch((err) => {
        if (!cancelled) {
          console.error('Failed to load references:', err);
          useAppStore.getState().showToast(t('failedToLoadReferences'), 'error');
        }
      }).finally(() => {
        if (!cancelled) setRefsLoading(false);
      });
    }

    return () => { cancelled = true; };
  }, [currentDocument?.document_id, currentProject?.index_doc_id, isPublicShare, publicToken, setReferences, t, referencesReloadKey, showArchived]);

  useEffect(() => {
    // PUBLIC: the agent-config endpoint is authed; skip on /s/:token (the
    // "Send to agent" button is canEdit-gated and wouldn't show anyway).
    if (isPublicShare || showAgentConfig || !currentDocument) return;
    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout>;
    const docId = currentDocument.document_id;
    // INVARIANT: a FAILED agent-config load must not be treated as "no agent configured".
    // Why: the catch used to latch hasAgentConfig=false, so a transient failure (backend
    // container restart drops the in-flight request) silently hid the run-pipeline button
    // until the user switched documents (No silent degradation, CLAUDE.md). Retry instead;
    // only an actual empty response (data falsy) hides the button.
    const check = (attempt = 0) => {
      apiClient.get(`/agent-config?document_id=${docId}`)
        .then((data) => {
          if (!cancelled) setHasAgentConfig(!!data);
        })
        .catch(() => {
          if (!cancelled && attempt < 5) retryTimer = setTimeout(() => check(attempt + 1), 1500);
        });
    };
    check();
    return () => { cancelled = true; clearTimeout(retryTimer); };
  }, [showAgentConfig, currentDocument?.document_id, isPublicShare]);

  const documentTitleById = useMemo(
    () => new Map(documents.map(d => [d.document_id, d.title])),
    [documents],
  );
  // WHY: labels an out-of-scope reference with its (foreign) parent — the visible half of
  // the stale-until-reload feature. A null label means "belongs to the current doc".
  const getLabelForReference = useCallback((ref: Reference) => {
    if (!ref.document_id || ref.document_id === currentDocument?.document_id) return null;
    if (ref.document_id === currentProject?.index_doc_id) return currentProject.name;
    return documentTitleById.get(ref.document_id) ?? 'Unknown';
  }, [currentDocument?.document_id, currentProject?.index_doc_id, currentProject?.name, documentTitleById]);

  // Backend depth-tiered order: own refs first (manual order), then ancestor
  // refs by proximity (immediate parent before grandparent, …), (sort_key, id)
  // ASC within each tier — a content edit no longer moves a ref. The store
  // preserves this LIST order between fetches (mergeRefLists maps over incoming;
  // addReference/placeReference place a ref inside ITS group's run; updateReference
  // mutates in place). Do NOT re-sort client-side.
  // WHY: never re-sort across tiers — the authoritative reference order is
  // the backend LIST (own → ancestors by proximity, manual key order within each
  // tier). Why: re-sorting client-side collapses depth tiers into a flat list,
  // breaking ancestry grouping.
  // PUBLIC carve-out: the panel RENDER list is
  // a pure order-preserving projection to {currentDoc} ∪ ancestors — a filter, never
  // a re-sort; the backend's tier order is already exactly right for that subset.
  // The WHY above protects the AUTHED stale-until-reload semantics (live moves
  // persist with a parent label until reload) — it does not apply to public: no WS
  // events, no mutations, and the list re-fetches on every doc switch. The STORE list
  // stays whole-subtree either way (public transclusion, hydrateReference, hover
  // previews and ImageGallery labels read it — see usePublicTransclusionSync).
  const sortedRefs = useMemo(
    () => (isPublicShare ? publicPanelReferences(references, documents, currentDocId) : [...references]),
    [isPublicShare, references, documents, currentDocId],
  );
  const nonImageRefs = useMemo(() => sortedRefs.filter(r => r.media_type !== 'image'), [sortedRefs]);
  const imageRefs = useMemo(() => sortedRefs.filter(r => r.media_type === 'image'), [sortedRefs]);
  // Rendered-scope count for the loading/empty gates: on public the VISIBLE list is
  // the projection — a doc whose scope holds zero refs shows the empty state, not
  // foreign refs behind an "empty" label. Authed: the raw store list (unchanged).
  const visibleCount = isPublicShare ? sortedRefs.length : references.length;

  const { scheduleDelete } = useReferenceDelete();

  // Manual order drag: reorder a ref WITHIN its own group. Works in preview mode too — images then live in ImageGallery,
  // but gallery tiles carry no row attributes, so only the visible non-image
  // RefCards are draggable/targets and keys are computed server-side over the
  // FULL live group (ordering around invisible image members is harmless).
  // Disabled on public share (no write surface) and for non-full roles (the
  // route gates too, defense in depth).
  // WHY: `!panelRef` is part of the gate, not decoration — the PillList unmounts
  // while a ref is open in panel mode and remounts as a NEW node on return; the
  // hook binds its listeners to the node present when its effect runs, so the
  // gate flip is what re-runs it against the fresh node (otherwise drag is dead
  // until the panel remounts).
  const listRef = useRef<HTMLDivElement>(null);
  const dragEnabled = canEdit && !isPublicShare && !panelRef;
  useSiblingDragReorder(listRef, dragEnabled, refDragAdapter);

  // Live table list for the current document, derived from the document editor's ydoc.
  // see SYSTEM: table-block — tables are listed ABOVE references (document/anchor order) as
  // badges. Empty before the collab handle publishes (no badges yet — distinct from error).
  const tables = useDocumentTables(currentDocId);

  // ARCH: the LIVE label for the focused table. The store's `currentTable` carries only
  // `{ document_id, table_id }`; the displayed label is resolved HERE from the reactive
  // `tables` list so a rename (local OR by a collaborative peer) never leaves a stale label
  // in the focus banner / header / split banner. The panel already re-renders on every
  // content change (useDocumentTables observes `content`), so maintaining the field here adds
  // zero extra re-renders. A ref-guarded bail-out writes the store ONLY when the label
  // actually changed (cell edits re-derive `tables` but leave the label untouched → no storm).
  const openTableId = currentTable?.table_id ?? null;
  const lastTableLabelRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const entry = openTableId ? tables.find(t => t.table_id === openTableId) ?? null : null;
    const next = entry?.label ?? null;
    if (lastTableLabelRef.current !== next) {
      lastTableLabelRef.current = next;
      setCurrentTableLabel(next);
    }
  }, [tables, openTableId, setCurrentTableLabel]);

  const handleTableSelect = useCallback((entry: DocumentTableEntry) => {
    if (!currentDocument) return;
    // ARCH: a table focus and a reference focus are mutually exclusive center views —
    // opening a table clears any open reference (and vice versa, see reference nav).
    if (currentReference) setCurrentReference(null);
    setCurrentTable({
      document_id: currentDocument.document_id,
      table_id: entry.table_id,
    });
    // Set the label SYNCHRONOUSLY with the open (No silent degradation: the banner/header
    // must never show the previously-open table's label for this one). The reactive effect
    // below then keeps it in sync for a later rename (local OR peer).
    setCurrentTableLabel(entry.label);
  }, [currentDocument, currentReference, setCurrentReference, setCurrentTable, setCurrentTableLabel]);

  // Table-badge ops (delete/insert/download/rename) — identity-scoped to the document's
  // ENTITY handle (see useTableBadgeOps): the focused slot may hold the open reference's
  // handle in split view and must never target the wrong ydoc.
  const {
    handleTableDelete,
    handleTableInsert,
    handleTableDownload,
    handleTableRename,
  } = useTableBadgeOps({
    currentDocument,
    currentTable,
    setCurrentTable,
    getView,
    tableRenameValue,
    setTableRenamingId,
  });

  const handleAdd = async () => {
    if (!currentProject || !currentDocument) return;
    await createMarkdownReference({
      projectId: currentProject.project_id,
      documentId: currentDocument.document_id,
    });
  };

  const handleDelete = async (refId: string) => {
    const state0 = useAppStore.getState();
    const refFound = state0.references.find(r => r.reference_id === refId);
    if (state0.accessLevel !== 'full') return;
    if (!refFound) return;
    removeReference(refId);
    if (currentReference?.reference_id === refId) setCurrentReference(null);
    getView()?.focus();
    scheduleDelete(refFound);
  };

  const retryTranscription = async (ref: Reference) => {
    try {
      await apiClient.post(`/references/${ref.reference_id}/retry`, {});
      updateReference(ref.reference_id, { processing_status: 'queued', content: '' });
    } catch (err) {
      console.error('Failed to retry transcription', err);
      useAppStore.getState().showToast(t('retryTranscriptionFailed'), 'error');
    }
  };

  const handleRetry = async (e: React.MouseEvent, ref: Reference) => {
    e.stopPropagation();
    await retryTranscription(ref);
  };

  const handleRenameRef = async (ref: Reference) => {
    const trimmed = renameValue.trim();
    if (!trimmed || trimmed === ref.title) { setRenamingId(null); return; }
    if (useAppStore.getState().accessLevel !== 'full') { setRenamingId(null); return; }

    setRenamingId(null);
    const isCurrent = currentReference?.reference_id === ref.reference_id;
    const setter = (r: Reference) => {
      updateReference(r.reference_id, r);
      if (isCurrent) setCurrentReference(r);
    };
    await withOptimistic(
      { ...ref, title: trimmed },
      ref,
      setter,
      () => apiClient.patch(`/references/${ref.reference_id}`, { title: trimmed }),
    );
  };

  const handleChangeRefParent = async (newDocumentId: string | null) => {
    if (!refParentPicker) return;
    const ref = refParentPicker.reference;
    try {
      const updated = await apiClient.patch(`/references/${ref.reference_id}`, { document_id: newDocumentId ?? '' });
      updateReference(ref.reference_id, updated);
      if (currentReference?.reference_id === ref.reference_id) {
        setCurrentReference(updated);
      }
    } catch (err) {
      console.error('Failed to change reference parent', err);
      useAppStore.getState().showToast(t('changeReferenceParentFailed'), 'error');
    }
    setRefParentPicker(null);
  };

  // staged-delete stage-1 handlers. Optimistic in-place
  // updateReference (position-preserving → dim-in-place, no reorder) + reconcile the
  // server PATCH response so updated_at stays truthful. Rollback on error (no-silent-
  // degradation). The optimistic patch does NOT re-issue the LIST — the card stays put,
  // dimmed, until the next reload/refetch (intended). The WS reference_updated echo
  // flows through useReferenceEvents → updateReference (archive) / bumpReferencesReload
  // (restore on a client that had filtered it out).
  const handleArchive = async (ref: Reference) => {
    const prev = ref.archived;
    updateReference(ref.reference_id, { archived: true });
    try {
      const updated = await apiClient.patch(`/references/${ref.reference_id}`, { archived: true });
      updateReference(ref.reference_id, updated);
      if (currentReference?.reference_id === ref.reference_id) setCurrentReference(updated);
    } catch (err) {
      console.error('Failed to archive reference', err);
      updateReference(ref.reference_id, { archived: prev });
      useAppStore.getState().showToast(t('failedToArchiveReference'), 'error');
    }
  };

  const handleRestore = async (ref: Reference) => {
    const prev = ref.archived;
    updateReference(ref.reference_id, { archived: false });
    try {
      const updated = await apiClient.patch(`/references/${ref.reference_id}`, { archived: false });
      updateReference(ref.reference_id, updated);
      if (currentReference?.reference_id === ref.reference_id) setCurrentReference(updated);
    } catch (err) {
      console.error('Failed to restore reference', err);
      updateReference(ref.reference_id, { archived: prev });
      useAppStore.getState().showToast(t('failedToRestoreReference'), 'error');
    }
  };

  const handleSendToAgent = async (ref: Reference) => {
    setAgentRunningRef(ref.reference_id);
    try {
      await apiClient.post('/agent-config/run', { reference_id: ref.reference_id });
      useAppStore.getState().showToast(t('agentRunAccepted'), 'info');
    } catch {
      useAppStore.getState().showToast(t('agentRunFailed'), 'error');
    } finally {
      setAgentRunningRef(null);
    }
  };

  // ARCH: "Chat with Reference" button handler.
  //
  // Delegates to chat-store.openChatWithReference — a single composite action
  // that serializes the entire flow (set ref → load sessions → add ref to
  // context → open panel). No second useEffect races with it. See
  // openChatWithReference.test.ts for the regression guard.
  const handleChatWithReference = (ref: Reference) =>
    useChatStore.getState().openChatWithReference(ref);

  const handleRefClick = useCallback((ref: Reference) => {
    if (ref.processing_status === 'uploading') return;
    // see SYSTEM: inbox — read = OPENED: selecting the reference clears the
    // viewer's flag (decrement keyed on the ref's OWN document — an ancestor
    // ref flags its own doc's pool, not the focused one). Fire-and-forget;
    // openInboxObject reports its own failure, ws:inbox_changed reconciles.
    if (ref.unread) {
      void openInboxObject(ref.document_id ?? currentDocument?.document_id, 'ref', ref.reference_id);
    }
    if (currentReference?.reference_id === ref.reference_id) {
      emit('reset-reference-banner');
      return;
    }
    emit('navigate-to-reference', { referenceId: ref.reference_id, stayInContext: true });
  }, [currentReference?.reference_id, currentDocument?.document_id]);

  // see SYSTEM: inbox — opening the refs tab while flagged references exist
  // opens the EARLIEST flagged one (created_at ASC). Gates mirror the panel's
  // own scope-settling: refsLoading (fetch in flight) and isFading (the render
  // after a doc switch still shows the previous doc's list — prevDocIdRef is
  // reconciled only when the load lands). Skips panel mode with a reference
  // already open and fires once per arrival (claimAutoOpen in inbox-store).
  // Public share has no viewer → no unread fields → natural no-op.
  useEffect(() => {
    const docId = currentDocId;
    if (!docId || isPublicShare || refsLoading || isFading || panelRef) return;
    const earliest = sortedRefs
      .filter(r => r.unread === true)
      .sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''))[0];
    if (!earliest) return;
    if (!claimAutoOpen('ref', docId)) return;
    handleRefClick(earliest);
  }, [currentDocId, isPublicShare, refsLoading, isFading, panelRef, sortedRefs, handleRefClick]);

  // ONE toggle element for both mounts (editor toolbar +
  // read-only row) so the icon, the tooltip and the on/off styling cannot drift apart.
  // The count (N) is folded into the title tooltip ONLY when ON — the OFF fetch excludes
  // archived refs, so it would always read 0.
  const archiveToggle = (
    <IconButton
      size="sm"
      title={showArchived ? `${t('hideArchived')} (${archivedCount})` : t('showArchived')}
      className={showArchived ? ACTIVE_TOGGLE_CLS : undefined}
      onClick={() => setShowArchived(!showArchived)}
    >
      <Archive size={13} />
    </IconButton>
  );

  return (
    <div
      data-testid="refs-panel-root"
      className="flex flex-col h-full transition-[background] duration-200"
      {...dragHandlers}
    >
      {panelRef && (
        <RefPanelPlaque
          reference={panelRef}
          canEdit={canEdit}
          currentDocumentId={currentDocument?.document_id ?? null}
          onBack={() => setCurrentReference(null)}
          onExitPanel={() => { if (currentDocId) setRefOpenMode(currentDocId, 'center'); }}
          // Go to the parent AND keep the reference open in the right panel: the parent
          // doc is switched to 'panel' mode, the reference rides the document commit as
          // pendingReference, and the jump itself is a document navigation (the chat
          // header's path).
          // WHY not navigate-to-reference: in 'panel' mode its resolver returns
          // stay-in-context for every ref (resolveReferenceNav), so it never leaves the
          // current document and the button did nothing.
          onGotoParent={documentId => {
            setRefOpenMode(documentId, 'panel');
            pinRightPanel(documentId, 'refs');
            useAppStore.getState().setPendingReference(panelRef);
            useAppStore.setState({ referenceSourceDocId: null });
            emit('navigate-to-document', { documentId });
          }}
          onChangeParent={(r, rect) => setRefParentPicker({ reference: r, anchorRect: rect })}
          onArchive={handleArchive}
          onRestore={handleRestore}
          onDelete={handleDelete}
          renamingId={renamingId}
          renameValue={renameValue}
          onStartRename={(r) => { setRenamingId(r.reference_id); setRenameValue(r.title); }}
          onRenameChange={setRenameValue}
          onRenameCommit={handleRenameRef}
          onRenameCancel={() => setRenamingId(null)}
        />
      )}
      {panelRef && (
        <div className="refs-panel-preview flex-1 flex flex-col overflow-hidden min-h-0 bg-bg">
          {/* Audio and file refs get their media bar here (the archive card carries
              its own file actions); images preview inside the secondary editor
              already, same guard as SplitEditorLayout. */}
          {(panelRef.media_type === 'audio' || panelRef.media_type === 'file') && (
            <ReferenceMediaBar reference={panelRef} canEdit={canEdit} />
          )}
          <Editor entity={panelRef} role="secondary" hideBanner />
        </div>
      )}

      {!panelRef && (canEdit || showAgentConfig) && (
        <div className="flex items-center gap-1 px-3 py-2 border-b border-border bg-surface min-h-[40px]">
          {showAgentConfig ? (
            <>
              <Button variant="ghost" size="sm" onClick={() => setShowAgentConfig(false)}>
                <ArrowLeft size={13} />
                {t('references')}
              </Button>
              <div className="flex-1" />
              <Button variant="ghost" size="sm" onClick={() => {
                const mgr = document.querySelector('[data-add-agent]') as HTMLButtonElement;
                mgr?.click();
              }}>
                <Plus size={13} />
                {t('addAgent')}
              </Button>
            </>
          ) : canEdit ? (
            <>
              <Button variant="ghost" size="sm" onClick={handleAdd}>
                <Plus size={13} />
                {t('add')}
              </Button>
              <Button variant="ghost" size="sm" onClick={() => fileInputRef.current?.click()}>
                <Upload size={13} />
                {t('upload')}
              </Button>
              {archiveToggle}
              {/* Compact viewport always opens references in the center — no mode to pick. */}
              {!compactLayout && (
                <>
                  <IconButton
                    size="sm"
                    title={t('toggleSplitView')}
                    className={modeActive('split') ? ACTIVE_TOGGLE_CLS : undefined}
                    onClick={() => toggleMode('split')}
                  >
                    <Columns2 size={13} />
                  </IconButton>
                  <IconButton
                    size="sm"
                    title={t('toggleRefInPanel')}
                    className={modeActive('panel') ? ACTIVE_TOGGLE_CLS : undefined}
                    onClick={() => toggleMode('panel')}
                  >
                    <Eye size={13} />
                  </IconButton>
                </>
              )}
              <div className="flex-1" />
              <Button variant="ghost" size="sm" onClick={() => setShowAgentConfig(true)}>
                <Bot size={13} />
                {t('agentConfig')}
              </Button>
            </>
          ) : null}
        </div>
      )}

      {/* Plan reference-archive-v2: "Show archived" is a READ filter → available to every
          role (incl. readonly/commentator viewers). Editors get `archiveToggle` in the
          toolbar above; read-only roles get the SAME element in this slim row. EXCLUDES the
          public share: the anonymous endpoint never serves archived refs by design, so the
          toggle would be a no-op (and a misleading one) there. */}
      {!panelRef && !canEdit && !showAgentConfig && currentDocument && !isPublicShare && (
        <div className="flex items-center justify-end gap-1 px-3 py-2 border-b border-border bg-surface min-h-[40px]">
          {archiveToggle}
        </div>
      )}

      <input
        ref={fileInputRef}
        type="file"
        accept="text/*,.md,.markdown,.txt,.rst,.tex,.csv,.json,.yaml,.yml,.log,.docx,.pdf,.webm,.ogg,.mp3,.wav,.m4a,.mp4,.jpg,.jpeg,.png,.gif,.webp"
        className="hidden"
        onChange={handleFileInput}
      />

      {!panelRef && <PillList ref={listRef} className={isDragging ? 'bg-accent-soft' : undefined}>
        {showAgentConfig && currentDocument && (
          <TranscriptionAgentConfigManager documentId={currentDocument.document_id} />
        )}

        {!showAgentConfig && (() => {
          const renderCard = (ref: Reference) => (
            <RefCard
              key={ref.reference_id}
              reference={ref}
              fading={isFading}
              isActive={currentReference?.reference_id === ref.reference_id}
              canEdit={canEdit}
              renamingId={renamingId}
              renameValue={renameValue}
              scopeLabel={getLabelForReference(ref)}
              onSelect={handleRefClick}
              onDelete={handleDelete}
              onArchive={canEdit ? handleArchive : () => {}}
              onRestore={canEdit ? handleRestore : () => {}}
              onStartRename={(r) => { setRenamingId(r.reference_id); setRenameValue(r.title); }}
              onRenameChange={setRenameValue}
              onRenameCommit={handleRenameRef}
              onRenameCancel={() => setRenamingId(null)}
              onRetry={handleRetry}
              onRetryConfirm={(r) => setRetryRef(r)}
              onChangeParent={(r, rect) => setRefParentPicker({ reference: r, anchorRect: rect })}
              onSendToAgent={hasAgentConfig ? handleSendToAgent : undefined}
              // WHY undefined on public share: the chat store is authed and the
              // public surface has no chat panel — RefCard already renders the
              // button only when this prop is defined, so undefined removes it
              // entirely. Never rely on UI hiding alone (CLAUDE.md): the handler
              // is also unreachable anonymously (no chat session exists).
              onChatWithReference={isPublicShare ? undefined : handleChatWithReference}
              agentRunning={hasAgentConfig && agentRunningRef === ref.reference_id}
              onRefHover={handleRefHover}
              onRefHoverLeave={hover.handleHoverLeave}
            />
          );

          // see SYSTEM: table-block — tables render ABOVE references (document/anchor order)
          // as badges, mixed into the same list. They appear in both modes (a table is
          // neither an image nor a markdown ref). See handleTableSelect/handleTableDelete.
          const renderTable = (entry: DocumentTableEntry) => (
            <TableBadge
              key={`table:${entry.table_id}`}
              entry={entry}
              isActive={currentTable?.table_id === entry.table_id}
              canEdit={canEdit}
              onSelect={handleTableSelect}
              onDelete={handleTableDelete}
              onInsert={handleTableInsert}
              onDownload={handleTableDownload}
              renamingId={tableRenamingId}
              renameValue={tableRenameValue}
              onStartRename={(e) => { setTableRenamingId(e.table_id); setTableRenameValue(e.label); }}
              onRenameChange={setTableRenameValue}
              onRenameCommit={handleTableRename}
              onRenameCancel={() => setTableRenamingId(null)}
            />
          );
          const tableBadges = tables.map(renderTable);

          // Backend depth-tiered order (own → ancestors by proximity, newest
          // within each tier). In preview mode images move to the ImageGallery
          // (unchanged); otherwise every ref renders as a RefCard. Tables lead in both.
          if (!previewMode) {
            return <>{tableBadges}{sortedRefs.map(renderCard)}</>;
          }

          return (
            <>
              {tableBadges}
              {nonImageRefs.map(renderCard)}
              <ImageGallery
                references={imageRefs}
                activeRefId={currentReference?.reference_id ?? null}
                canEdit={canEdit}
                getLabel={getLabelForReference}
                onSelect={handleRefClick}
                onDelete={handleDelete}
                onArchive={canEdit ? handleArchive : () => {}}
                onRestore={canEdit ? handleRestore : () => {}}
                onChangeParent={(r, rect) => setRefParentPicker({ reference: r, anchorRect: rect })}
                onRefHover={handleRefHover}
                onRefHoverLeave={hover.handleHoverLeave}
              />
            </>
          );
        })()}

        {/* INVARIANT: show the spinner (not the empty state) while the scope fetch is
            in flight with nothing yet to show — empty state ≠ loading state.  Why: while refs fetch with nothing shown, a spinner (not empty state) keeps loading distinct from empty; tables may already be present from the live ydoc. Tables come
            from the live ydoc and may already be present while refs fetch. Gated on
            the RENDERED scope (visibleCount): on public the store list is whole-subtree
            (transclusion) while the panel shows only doc+ancestors. */}
        {!showAgentConfig && refsLoading && visibleCount === 0 && tables.length === 0 && (
          <PanelLoading />
        )}

        {!showAgentConfig && !isFading && !refsLoading && visibleCount === 0 && tables.length === 0 && (
          <div className="py-6 px-2 text-ui-base text-text-dim text-center border-2 border-dashed border-border mt-2">
            {t('dragAndDrop')}<br/>{t('orUseButtonsAbove')}
          </div>
        )}
      </PillList>}

      <Modal open={!!retryRef} onClose={() => setRetryRef(null)} title={t('rerunTranscription')} width={380} closeLabel={t('close')}
        footer={<>
          <Button variant="ghost" onClick={() => setRetryRef(null)}>{t('cancel')}</Button>
          <Button
            variant="primary"
            onClick={async () => {
              const ref = retryRef!;
              setRetryRef(null);
              await retryTranscription(ref);
            }}
          >
            {t('retranscribe')}
          </Button>
        </>}
      >
        {retryRef && (
          <div className="text-ui-base text-text-muted leading-relaxed">
            {t('retranscribeWarning', { title: retryRef.title })}
            {' '}{t('retranscribeWarning2')}
          </div>
        )}
      </Modal>

      {refParentPicker && (
        <ParentPickerPopup
          currentDocumentId={refParentPicker.reference.document_id}
          anchorRect={refParentPicker.anchorRect}
          onClose={() => setRefParentPicker(null)}
          onMoved={handleChangeRefParent}
        />
      )}
      <HoverPreviewPopup
        hover={hover}
        title={hoverRef?.title}
        content={hoverContent}
        imageUrl={hoverImageUrl}
        error={hoverError}
        loading={hoverLoading}
      />
    </div>
  );
}
