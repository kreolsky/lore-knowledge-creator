/** Checkpoint history panel: merged timeline of snapshots + document history, PDF/DOCX export, hover preview. */
// ARCH: Single merged timeline (snapshots + document_history) sorted by created_at desc.
// ARCH: Snapshot rows are clickable (preview) and hoverable (LinkPreviewPopup excerpt).
// ARCH: History rows are non-interactive (cursor: default, no hover popup).
// SYSTEM: checkpoint-history — merged snapshot + history timeline with export

import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { Plus, Pencil, History, ClipboardList } from 'lucide-react';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { readRefOpenMode, refIsScope } from '../store/ui-store/documents-slice';
import { useShallow } from 'zustand/react/shallow';
import { apiClient } from '../api/client';
import { useEvent } from '../hooks/useEvent';
import { useHoverPreview } from '../hooks/useHoverPreview';
import { useTranslation } from '../i18n';
import { useEditorContent, getRoleView } from '../editor/active-editor';
import type { Checkpoint, DocumentHistoryEntry } from '../types';
import { formatDate } from '../utils/format';
import { Button, IconButton, FieldTextarea, DownloadMenu, PanelLoading } from './ui';
import { PREVIEW_WIDTH } from '../utils/preview-geometry';
import { HoverPreviewPopup } from './HoverPreviewPopup';
import { upsertSnapshotById, appendDedupSnapshots } from './history-panel-helpers';
import { LABEL_LAST_SESSION as LAST_SESSION_LABEL } from '../collab/backup-labels';
import { getEntityHandle } from '../collab/active-handle-registry';
import { serializeTables } from './editor/live-preview/table-block-model';
import { useListCapacity } from './chat/useListCapacity';
import { useSwipePager } from '../hooks/useSwipePager';

type TimelineEntry =
  | { kind: 'snapshot'; data: Checkpoint }
  | { kind: 'history'; data: DocumentHistoryEntry };

// Used only before the first height measurement / when ResizeObserver is unavailable.
const FALLBACK_CAPACITY = 7;

function SnapshotActions({ onEdit, isSelected }: { onEdit: () => void; isSelected: boolean }) {
  const { t } = useTranslation();
  // Selected snapshot keeps the green selection tint behind the edit icon; unselected stays gray.
  const fadeColor = isSelected ? 'var(--sticky-green-dark)' : 'var(--surface3)';
  return (
    <div className="absolute right-0 top-0 bottom-0 flex items-center gap-0.5 pl-4 pr-1 opacity-0 group-hover/row:opacity-100" style={{ background: `linear-gradient(to right, transparent 0px, ${fadeColor} 16px)` }} onClick={e => e.stopPropagation()}>
      <IconButton size="sm" title={t('editComment')} onClick={e => { e.stopPropagation(); onEdit(); }}>
        <Pencil size={13} />
      </IconButton>
    </div>
  );
}

export function HistoryPanel() {
  const { currentDocument, rawReference, snapshotPreview, setSnapshotPreview, openSnapshotModal } = useAppStore(useShallow(s => ({
    currentDocument: s.currentDocument, rawReference: s.currentReference,
    snapshotPreview: s.snapshotPreview, setSnapshotPreview: s.setSnapshotPreview,
    openSnapshotModal: s.openSnapshotModal,
  })));
  // Panel quick preview: the Checkpoint tab is the DOCUMENT's — a previewed
  // reference is not the scope, so it does not lock the panel to
  // "history not available".
  const refOpenMode = useUIStore(s => readRefOpenMode(s.documents[currentDocument?.document_id ?? ''], s.compactLayout));
  const currentReference = refIsScope(refOpenMode) ? rawReference : null;
  const showToast = useAppStore(s => s.showToast);
  const getContent = useEditorContent();
  const [snapshots, setSnapshots] = useState<Checkpoint[]>([]);
  const [entries, setEntries] = useState<DocumentHistoryEntry[]>([]);
  const [historyError, setHistoryError] = useState(false);
  // INVARIANT: gate the "no snapshots yet" empty state on this — timeline.length===0
  // is also true while /checkpoints + /history are in flight. Empty ≠ loading.  Why: timeline.length===0 is true both when empty and while the fetches are in flight; gating the empty state on timelineLoading keeps empty distinct from loading (no silent degradation).
  const [timelineLoading, setTimelineLoading] = useState(false);
  // Anchored pagination: the state is the INDEX of the top
  // visible row, not a page number. On a capacity change (resize) `start` is left
  // untouched so the top row stays put. `hasMoreSnapshots` drives the server top-up
  // (next offset chunk of 200) once the user reaches the end of the loaded timeline.
  // The full snapshot array is the source of truth — the pager is pure derived state
  // (upserts keep working).
  const [start, setStart] = useState(0);
  const [hasMoreSnapshots, setHasMoreSnapshots] = useState(false);
  const [snapshotsOffset, setSnapshotsOffset] = useState(0);
  const selectedId = snapshotPreview?.checkpoint_id ?? null;
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editComment, setEditComment] = useState('');
  const { t } = useTranslation();
  const isReference = !!currentReference;
  const currentDocId = currentDocument?.document_id ?? null;

  // INVARIANT: Shared cache Map for hover+click lazy content fetch.  Why: hover and click both lazy-fetch snapshot content; one shared Map avoids refetching the same snapshot on hover-then-click.
  const contentCache = useRef<Map<string, Checkpoint>>(new Map());

  // WHY: t() is recreated every render; keep it out of data-fetch callback deps to
  // avoid an infinite reload loop (mount effect keyed on the callbacks). Read via ref.
  const tRef = useRef(t);
  tRef.current = t;

  const loadSnapshots = useCallback(async () => {
    if (currentDocId) {
      try {
        const data = await apiClient.get(`/checkpoints?document_id=${currentDocId}`) as Checkpoint[];
        setSnapshots(data);
        setSnapshotsOffset(data.length);
        setHasMoreSnapshots(data.length === 200);
      } catch (err) {
        console.error('Failed to load snapshots', err);
        showToast(tRef.current('snapshotLoadFailed'), 'error');
      }
    }
  }, [currentDocId, showToast]);

  // Server top-up — fetch the next offset chunk of 200 snapshots (deduped by id
  // against the already-loaded array) when the user reaches the last loaded page and
  // the backend indicated more exist. Bounded by the snapshot count; history rows are
  // a single fetch (memory not bounded, rendering is — see the plan's "Not doing").
  const loadingMoreRef = useRef(false);
  const loadMoreSnapshots = useCallback(async () => {
    if (!currentDocId || loadingMoreRef.current || !hasMoreSnapshots) return;
    loadingMoreRef.current = true;
    try {
      const nextOffset = snapshotsOffset;
      const data = await apiClient.get(`/checkpoints?document_id=${currentDocId}&offset=${nextOffset}`) as Checkpoint[];
      setSnapshots(prev => appendDedupSnapshots(prev, data));
      setSnapshotsOffset(nextOffset + data.length);
      setHasMoreSnapshots(data.length === 200);
    } catch (err) {
      console.error('Failed to load more snapshots', err);
      useAppStore.getState().showToast(tRef.current('snapshotLoadFailed'), 'error');
    } finally {
      loadingMoreRef.current = false;
    }
  }, [currentDocId, hasMoreSnapshots, snapshotsOffset]);

  const loadHistory = useCallback(() => {
    if (!currentDocId) return Promise.resolve();
    setHistoryError(false);
    return apiClient
      .get(`/documents/${currentDocId}/history`)
      .then((data) => setEntries(data as DocumentHistoryEntry[]))
      .catch(() => setHistoryError(true));
  }, [currentDocId]);

  useEffect(() => {
    setSnapshotPreview(null);
    contentCache.current.clear();
    setStart(0);
    setHasMoreSnapshots(false);
    setSnapshotsOffset(0);
    if (!currentDocId) { setTimelineLoading(false); return; }
    let cancelled = false;
    setTimelineLoading(true);
    // WHY: settle the gate only once BOTH the snapshots and history fetches
    // finish — neither loader rejects (both catch internally), so allSettled here
    // is purely a "both done" join, not error handling.  Why: the timeline gate must clear only after BOTH fetches settle; allSettled (not all) because neither rejects — it is a join, not error handling.
    Promise.allSettled([loadSnapshots(), loadHistory()]).finally(() => {
      if (!cancelled) setTimelineLoading(false);
    });
    return () => { cancelled = true; };
  }, [loadSnapshots, loadHistory, currentDocId]);

  useEvent('snapshot-created', useCallback((snap: Checkpoint) => {
    // Upsert by checkpoint_id (replace in place), NOT dedup-and-drop. A last-session
    // row is upserted on session end and reuses the same checkpoint_id; dropping a
    // same-id payload would silently ignore the refreshed content/time.
    setSnapshots(prev => upsertSnapshotById(prev, snap));
    // An upsert refreshed an existing row's content — drop the cached preview blob so
    // a re-opened preview fetches the new content instead of the stale cached one.
    contentCache.current.delete(snap.checkpoint_id);
  }, []));

  useEvent('document-history-added', useCallback((entry: DocumentHistoryEntry) => {
    if (entry.document_id === currentDocId) {
      setEntries(prev => prev.some(e => e.id === entry.id) ? prev : [...prev, entry]);
    }
  }, [currentDocId]));

  const timeline = useMemo<TimelineEntry[]>(() => {
    const items: TimelineEntry[] = [
      ...snapshots.map(s => ({ kind: 'snapshot' as const, data: s })),
      ...entries.map(e => ({ kind: 'history' as const, data: e })),
    ];
    items.sort((a, b) => new Date(b.data.created_at).getTime() - new Date(a.data.created_at).getTime());
    return items;
  }, [snapshots, entries]);

  // Adaptive capacity: as many timeline rows as fit the panel height (measured, no
  // hardcoded pixels).
  const listRef = useRef<HTMLDivElement>(null);
  const capacity = useListCapacity(listRef, null, timeline.length, FALLBACK_CAPACITY);

  // Anchored-index pager. `safeStart` clamps when the timeline
  // shrinks (e.g. after a doc switch) so the index never runs out of bounds, WITHOUT
  // snapping to a full-page boundary — that would shift the anchored top row on resize.
  const safeStart = Math.max(0, Math.min(start, Math.max(0, timeline.length - 1)));
  // Render capacity: while the container is unmeasured (capacity === 0 — only when the
  // timeline was empty at mount, since useListCapacity's useState initial doesn't
  // re-apply on the async 0→N load), fall back to a provisional slice so rows exist for
  // useListCapacity to measure. Without this, slice(0, 0) renders no rows → nothing to
  // measure → capacity deadlocks at 0 (empty list + phantom "1/1" pager). No-op once
  // measured (capacity ≥ 1).
  const renderCapacity = capacity > 0 ? capacity : Math.min(timeline.length, FALLBACK_CAPACITY);
  const pageItems = timeline.slice(safeStart, safeStart + renderCapacity);
  const atStart = safeStart === 0;
  const atEnd = capacity > 0 && safeStart + capacity >= timeline.length;
  const totalPages = capacity > 0 ? Math.max(1, Math.ceil(timeline.length / capacity)) : 1;
  const pageNum = capacity > 0 ? Math.floor(safeStart / capacity) + 1 : 1;

  // Wheel/trackpad swipe paging (one gesture = one page).
  // Gated on a measured capacity so the unmeasured window never swallows wheel.
  useSwipePager(listRef, {
    onPrev: () => setStart(s => Math.max(0, s - capacity)),
    onNext: () => setStart(s => Math.min(Math.max(0, timeline.length - capacity), s + capacity)),
  }, capacity > 0 && timeline.length > capacity);

  // Top-up: when the user reaches the last loaded row and the backend indicated more
  // snapshots exist, fetch the next offset chunk. The freshly appended rows extend the
  // timeline for further paging.
  useEffect(() => {
    if (hasMoreSnapshots && atEnd && !timelineLoading) {
      void loadMoreSnapshots();
    }
  }, [atEnd, hasMoreSnapshots, timelineLoading, loadMoreSnapshots]);

  const fetchSnapshotContent = useCallback(async (snap: Checkpoint): Promise<Checkpoint> => {
    if (snap.content !== undefined) return snap;
    const cached = contentCache.current.get(snap.checkpoint_id);
    if (cached) return cached;
    try {
      const full = await apiClient.get(`/checkpoints/${snap.checkpoint_id}`) as Checkpoint;
      contentCache.current.set(snap.checkpoint_id, full);
      return full;
    } catch (err) {
      // WHY: no toast here — the click path (handleSelectSnapshot) toasts on the
      // content-less result, and the hover preview is passive: a toast per hovered
      // row would be noise, and the popup simply shows no body.
      console.error('Failed to load snapshot content', err);
      return snap;
    }
  }, []);

  const handleSelectSnapshot = async (snap: Checkpoint) => {
    const full = await fetchSnapshotContent(snap);
    if (full.content === undefined) {
      showToast(t('snapshotContentFailed'), 'error');
      return;
    }
    setSnapshotPreview(full);
    setSnapshots(prev => prev.map(s => s.checkpoint_id === snap.checkpoint_id
      ? { ...full, user_name: full.user_name ?? s.user_name } : s));
  };

  const handleEditSubmit = async (id: string) => {
    try {
      const updated = await apiClient.patch(`/checkpoints/${id}`, { comment: editComment });
      setSnapshots(prev => prev.map(s => s.checkpoint_id === id ? updated as Checkpoint : s));
      if (selectedId === id) setSnapshotPreview(updated as Checkpoint);
      contentCache.current.delete(id);
      setEditingId(null);
    } catch (err) {
      console.error('Failed to update snapshot', err);
      showToast(t('snapshotUpdateFailed'), 'error');
    }
  };

  // Hover preview for snapshot rows
  const getPopupLeft = useCallback((rect: DOMRect) => rect.left - PREVIEW_WIDTH - 8, []);
  const hover = useHoverPreview({ getPopupLeft });
  const [hoverTitle, setHoverTitle] = useState<string>('');
  // INVARIANT: each hover state field carries the snapshot id it describes and is
  // read only when that id still matches the hovered one (mirrors useDocumentPreview).
  // Why: the content fetch resolves after the pointer may have moved to another row —
  // an id-less body painted the previous snapshot's text under the new hover, and a
  // failed fetch painted '' which read as "No content" (error as empty, no-silent-degradation).
  const [hoverSnapId, setHoverSnapId] = useState<string | null>(null);
  const [hoverEntry, setHoverEntry] = useState<{ id: string; content: string } | undefined>(undefined);
  const [hoverErrorId, setHoverErrorId] = useState<string | null>(null);
  // The .then callback closes over a stale hoverSnapId state value — the guard
  // must read the LATEST hovered id, so it lives in a ref.
  const hoverSnapIdRef = useRef<string | null>(null);
  const hoverContent = hoverEntry?.id === hoverSnapId ? hoverEntry.content : undefined;
  const hoverError = hoverSnapId != null && hoverErrorId === hoverSnapId;
  const hoverLoading = hoverSnapId != null && hoverContent === undefined && !hoverError;

  // The row's label: last-session rows render the per-user display label BEFORE the
  // comment fallback — a null comment must not flash the manual-snapshot fallback string.
  const snapshotLabel = useCallback((snap: Checkpoint) => (
    snap.label === LAST_SESSION_LABEL
      ? t('lastSessionLabel', { name: snap.user_name ?? 'System' })
      : (snap.comment || t('snapshotManualFallback'))
  ), [t]);

  const handleRowHover = useCallback((el: HTMLElement, snap: Checkpoint) => {
    const id = snap.checkpoint_id;
    hoverSnapIdRef.current = id;
    setHoverSnapId(id);
    // Synchronous: the plaque labels the hover immediately — with the body shown
    // as loading until its fetch lands, plaque and body can no longer disagree.
    setHoverTitle(snapshotLabel(snap));
    // WHY: the id guard — a response for a snapshot that is no longer the hovered
    // one (hover A slow, hover B fast) must never overwrite B's body with A's text.
    fetchSnapshotContent(snap).then(full => {
      if (hoverSnapIdRef.current !== id) return;
      // Failure signal is content === undefined — the same one the click path
      // (handleSelectSnapshot) uses; the catch inside fetchSnapshotContent already
      // swallowed the error (no toast on hover), so the popup body is the error surface.
      if (full.content === undefined) {
        setHoverEntry(undefined);
        setHoverErrorId(id);
        return;
      }
      setHoverErrorId(null);
      setHoverEntry({ id, content: full.content.slice(0, 400) });
    });
    hover.handleHover(el);
  }, [hover, fetchSnapshotContent, snapshotLabel]);

  const handleRowHoverLeave = useCallback((e: React.MouseEvent) => {
    hover.handleHoverLeave(e);
  }, [hover]);

  if (isReference) {
    return (
      <div className="panel-body">
        <div className="py-4 px-2 text-ui-base text-text-dim text-center">
          {t('historyNotAvailable')}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Banner: Save snapshot + Download (left, grouped) */}
      <div className="flex items-center gap-1 px-3 py-2 border-b border-border bg-surface min-h-[40px]">
        {/* INVARIANT: the manual snapshot captures the DOCUMENT — content from the
            PRIMARY column's view, tables from the doc's ENTITY handle. Why: rootView
            and the focused slot both follow FOCUS, so with the reference focused the
            old pairing wrote ref content + ref tables into a DOC checkpoint; Cmd+S is
            a doc-column keymap (fires only with the primary focused) and stays
            slot-based. */}
        <Button variant="ghost" size="sm" onClick={() => {
          const primary = getRoleView('primary');
          const content = primary ? primary.state.doc.toString() : getContent();
          const handle = currentDocument ? getEntityHandle(currentDocument.document_id) : null;
          const tablesJson = handle?.ydoc ? serializeTables(handle.ydoc) : '{}';
          openSnapshotModal(content, tablesJson);
        }}>
          <Plus size={13} />
          {t('saveSnapshot')}
        </Button>
        <DownloadMenu documentId={currentDocId!} hasText theme={selectedId ? 'green' : 'default'} checkpointId={selectedId ?? undefined} />
      </div>

      {/* Timeline (overflow-hidden: capacity fits exactly; swipe owns paging) */}
      <div ref={listRef} className="flex-1 overflow-hidden py-1 px-1.5">
        {historyError && (
          <p className="text-ui-base text-text-dim text-center py-2 px-2">
            {t('infoHistoryFailed')}{' '}
            <Button type="button" variant="ghost" size="sm" onClick={loadHistory}>{t('retry')}</Button>
          </p>
        )}
        {!historyError && timelineLoading && timeline.length === 0 && (
          <PanelLoading />
        )}
        {!historyError && !timelineLoading && timeline.length === 0 && (
          <div className="py-4 px-2 text-ui-base text-text-dim text-center">
            {t('noSnapshotsYet')}
          </div>
        )}
        {!historyError && pageItems.map(item => {
          if (item.kind === 'history') {
            const e = item.data;
            const actionLabel = e.action === 'created' ? t('infoActionCreated') : t('infoActionEdited');
            return (
              <div key={`hist-${e.id}`} className="doc-item doc-item--right-panel cursor-default">
                <span className="w-3" />
                <span className="doc-icon"><ClipboardList size={14} /></span>
                <span className="doc-label">
                  <span className="note-time">{formatDate(e.created_at)}</span>
                  {' — '}
                  <span className="text-text font-medium">{e.user_name}</span>
                  <span className="text-text-dim ml-1">{actionLabel}</span>
                </span>
              </div>
            );
          }

          const snap = item.data;
          const isSelected = selectedId === snap.checkpoint_id;
          const isEditing = editingId === snap.checkpoint_id;
          const userName = snap.user_name ?? 'System';
          const isLastSession = snap.label === LAST_SESSION_LABEL;
          const displayComment = snapshotLabel(snap);

          if (isEditing) {
            return (
              <div key={`snap-${snap.checkpoint_id}`}
                className="doc-item doc-item--right-panel cursor-default bg-surface3"
              >
                <span className="flex-1 min-w-0" onClick={e => e.stopPropagation()}>
                  <FieldTextarea
                    className="min-h-[60px] mb-1.5 w-full"
                    value={editComment}
                    onChange={e => setEditComment(e.target.value)}
                    placeholder={t('commentOptional')}
                    autoFocus
                    onKeyDown={e => {
                      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') handleEditSubmit(snap.checkpoint_id);
                      if (e.key === 'Escape') setEditingId(null);
                    }}
                  />
                  <div className="flex justify-end gap-1.5">
                    <Button variant="ghost" onClick={() => setEditingId(null)}>{t('cancel')}</Button>
                    <Button variant="primary" onClick={() => handleEditSubmit(snap.checkpoint_id)}>{t('save')}</Button>
                  </div>
                </span>
              </div>
            );
          }

          return (
            <div key={`snap-${snap.checkpoint_id}`}
              className={`doc-item doc-item--right-panel group/row cursor-pointer${isSelected ? ' selected-snap bg-[var(--sticky-green-dark)]' : ''}`}
              onClick={() => handleSelectSnapshot(snap)}
              onMouseEnter={e => handleRowHover(e.currentTarget, snap)}
              onMouseLeave={handleRowHoverLeave}
            >
              <span className="w-3" />
              <span className="doc-icon"><History size={14} /></span>
              {/* doc-label owns the truncation ellipsis; its color must match the comment text so the ellipsis tints green when selected, gray otherwise. */}
              <span className={`doc-label${isSelected ? ' text-[var(--sticky-green-darkest)]' : ' text-text-dim'}`}>
                <span className="note-time">{formatDate(snap.created_at)}</span>
                {' — '}
                <span className="text-text font-medium">{userName}</span>
                <span className={`ml-1${isSelected ? ' text-[var(--sticky-green-darkest)]' : ' text-text-dim'}${snap.comment || isLastSession ? '' : ' italic'}`}>
                  {displayComment}
                </span>
              </span>
              <SnapshotActions isSelected={isSelected} onEdit={() => { setEditingId(snap.checkpoint_id); setEditComment(snap.comment || ''); }} />
            </div>
          );
        })}
      </div>

      {/* Pager over the merged timeline. */}
      {capacity > 0 && timeline.length > capacity && (
        <div className="flex items-center justify-center gap-2 px-3 py-2 bg-surface">
          <Button variant="ghost" size="sm" disabled={atStart} onClick={() => setStart(s => Math.max(0, s - capacity))}>
            {t('prevPage')}
          </Button>
          <span className="text-xs text-text-dim tabular-nums">
            {t('pageOf', { n: pageNum, total: totalPages })}
          </span>
          <Button variant="ghost" size="sm" disabled={atEnd && !hasMoreSnapshots} onClick={() => setStart(s => Math.min(Math.max(0, timeline.length - capacity), s + capacity))}>
            {t('nextPage')}
          </Button>
        </div>
      )}

      {/* Hover preview popup for snapshot rows */}
      <HoverPreviewPopup
        hover={hover}
        title={hoverTitle}
        content={hoverContent}
        error={hoverError}
        loading={hoverLoading}
      />
    </div>
  );
}
