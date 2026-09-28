/**
 * useTreeDragReorder: pointer-driven manual reordering of document rows WITHIN one
 * sibling level. Grab a row and drop it among its siblings; parent never changes.
 *
 * // ARCH: backend-authoritative ordering — on drop we send `after_id` (the sibling
 * // to land after, null = top) and the server computes the fractional sort_key,
 * // broadcasting it back. The optimistic update uses a provisional key purely for
 * // instant feedback and is reconciled by the PATCH response (rollback on error).
 *
 * Reuses the repo's PointerEvent drag pattern (see useResizer.ts): document-level
 * listeners, refs for transient state, visual feedback via imperative inline styles
 * (no new CSS classes), commit on pointerup.
 */
import { useEffect } from 'react';
import { useAppStore } from '../store/app-store';
import { apiClient } from '../api/client';
import { t } from '../i18n';
import type { Document } from '../types';

const DRAG_THRESHOLD = 5; // px before a press becomes a drag (vs a click)
const DRAG_SHADOW = 'inset 0 2px 0 0 var(--accent)';

/** Siblings of `parentId` (excluding `selfId`) in persisted order: sort_key, then id. */
function orderedSiblings(docs: Document[], parentId: string | null, selfId: string): Document[] {
  return docs
    .filter(d => !d.is_index && (d.parent_id ?? null) === parentId && d.document_id !== selfId)
    .sort((a, b) => {
      const ka = a.sort_key ?? '';
      const kb = b.sort_key ?? '';
      if (ka !== kb) return ka < kb ? -1 : 1;
      return a.document_id < b.document_id ? -1 : a.document_id > b.document_id ? 1 : 0;
    });
}

export function useTreeDragReorder(
  treeRef: React.RefObject<HTMLElement | null>,
  enabled: boolean,
) {
  useEffect(() => {
    if (!enabled) return;
    const container = treeRef.current;
    if (!container) return;

    let candidate: { id: string; parentId: string | null; x: number; y: number } | null = null;
    let dragging = false;
    let draggedRow: HTMLElement | null = null;
    let indicatorRow: HTMLElement | null = null;
    // after_id for the pending drop: string = after that sibling, null = top, undefined = no valid target
    let dropAfterId: string | null | undefined = undefined;

    const reorderableRow = (target: EventTarget | null): HTMLElement | null => {
      const el = target as HTMLElement | null;
      const row = el?.closest?.('[data-doc-id]') as HTMLElement | null;
      // Only rows with data-parent-id are reorderable (excludes the project-context row).
      return row && row.hasAttribute('data-parent-id') ? row : null;
    };

    const clearIndicator = () => {
      if (indicatorRow) indicatorRow.style.boxShadow = '';
      indicatorRow = null;
    };

    const onPointerDown = (e: PointerEvent) => {
      if (e.button !== 0) return;
      const tgt = e.target as HTMLElement;
      // Don't hijack action buttons, the rename input, or the gear menu.
      if (tgt.closest('.doc-item-actions') || tgt.closest('input')) return;
      const row = reorderableRow(tgt);
      if (!row) return;
      const id = row.getAttribute('data-doc-id');
      if (!id) return;
      candidate = { id, parentId: row.getAttribute('data-parent-id') || null, x: e.clientX, y: e.clientY };
    };

    const updateDropTarget = (e: PointerEvent) => {
      clearIndicator();
      dropAfterId = undefined;
      const row = reorderableRow(document.elementFromPoint(e.clientX, e.clientY));
      if (!row) return;
      const rowParent = row.getAttribute('data-parent-id') || null;
      const rowId = row.getAttribute('data-doc-id')!;
      // INVARIANT: only same-level siblings are valid drop targets (parent never changes).  Why: drag-reorder is constrained to same-level siblings (same parent) so reordering never changes a node's parent (tree structure stays intact).
      if (rowParent !== candidate!.parentId || rowId === candidate!.id) return;

      const rect = row.getBoundingClientRect();
      const before = e.clientY < rect.top + rect.height / 2;
      const siblings = orderedSiblings(useAppStore.getState().documents, candidate!.parentId, candidate!.id);
      const idx = siblings.findIndex(s => s.document_id === rowId);
      if (before) {
        dropAfterId = idx > 0 ? siblings[idx - 1].document_id : null;
        row.style.boxShadow = 'inset 0 2px 0 0 var(--accent)';
      } else {
        dropAfterId = rowId;
        row.style.boxShadow = 'inset 0 -2px 0 0 var(--accent)';
      }
      indicatorRow = row;
    };

    const onPointerMove = (e: PointerEvent) => {
      if (!candidate) return;
      if (!dragging) {
        if (Math.hypot(e.clientX - candidate.x, e.clientY - candidate.y) < DRAG_THRESHOLD) return;
        dragging = true;
        document.body.style.userSelect = 'none';
        draggedRow = container.querySelector<HTMLElement>(`[data-doc-id="${candidate.id}"]`);
        if (draggedRow) draggedRow.style.opacity = '0.5';
      }
      updateDropTarget(e);
    };

    const commitReorder = async (docId: string, oldKey: string | undefined, afterId: string | null) => {
      const docs = useAppStore.getState().documents;
      // Provisional optimistic key: '' floats to the top; appending a mid digit to the
      // anchor key lands just after it. Reconciled by the server response below.
      const anchor = afterId ? docs.find(d => d.document_id === afterId)?.sort_key ?? '' : '';
      const provisional = afterId ? `${anchor}V` : '';
      const apply = (key: string | undefined) =>
        useAppStore.getState().setDocuments(
          useAppStore.getState().documents.map(d => d.document_id === docId ? { ...d, sort_key: key } : d),
        );
      apply(provisional);
      try {
        const updated = await apiClient.patch(`/documents/${docId}/reorder`, { after_id: afterId });
        apply(updated?.sort_key ?? provisional);
      } catch (err) {
        console.error('Failed to reorder document', err);
        apply(oldKey); // rollback
        useAppStore.getState().showToast(t('failedToReorderDocument'), 'error');
      }
    };

    const endDrag = () => {
      const wasDragging = dragging;
      const cand = candidate;
      const after = dropAfterId;
      if (draggedRow) draggedRow.style.opacity = '';
      clearIndicator();
      document.body.style.userSelect = '';
      candidate = null;
      dragging = false;
      draggedRow = null;
      dropAfterId = undefined;
      if (!wasDragging || !cand || after === undefined) return;
      // Suppress the click that may fire after pointerup so the row doesn't navigate.
      // Browsers usually suppress click after pointer movement; this is defensive and
      // self-removes after a short window so it can never swallow a later real click.
      const swallow = (ev: Event) => { ev.stopPropagation(); ev.preventDefault(); };
      container.addEventListener('click', swallow, { capture: true, once: true });
      setTimeout(() => container.removeEventListener('click', swallow, true), 250);

      const self = useAppStore.getState().documents.find(d => d.document_id === cand.id);
      void commitReorder(cand.id, self?.sort_key, after);
    };

    container.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('pointermove', onPointerMove);
    document.addEventListener('pointerup', endDrag);
    return () => {
      container.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('pointermove', onPointerMove);
      document.removeEventListener('pointerup', endDrag);
      clearIndicator();
      document.body.style.userSelect = '';
    };
  }, [treeRef, enabled]);
}
