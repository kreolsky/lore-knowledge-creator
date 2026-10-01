/**
 * useSiblingDragReorder: pointer-driven manual reordering of rows WITHIN one
 * sibling group — tree documents or reference cards. Grab a row and drop it
 * among its siblings; the group never changes.
 *
 * // ARCH: backend-authoritative ordering — on drop we send `after_id` (the sibling
 * // to land after, null = top) and the server computes the fractional sort_key,
 * // broadcasting it back. The optimistic update uses a provisional key purely for
 * // instant feedback and is reconciled by the PATCH response (rollback on error).
 *
 * The KIND of rows is supplied by the adapter (row/group attributes, ordered
 * siblings, ignore-target predicate, commit) — the pointer mechanics are shared.
 * Two adapters = the real seam: tree docs and reference cards commit to the
 * same PATCH /documents/{id}/reorder but own different stores.
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

export interface DragSibling {
  id: string;
  sortKey: string | undefined;
}

export interface SiblingDragAdapter {
  /** Attribute holding the row id on each row element (e.g. 'data-doc-id'). */
  rowIdAttr: string;
  /** Attribute marking the row's group; rows without it are neither draggable nor targets. */
  groupAttr: string;
  /** Siblings of `groupId` (excluding `selfId`) in persisted order: sort_key, then id. */
  orderedSiblings(groupId: string | null, selfId: string): DragSibling[];
  /** True when a pointerdown on `el` must not start a drag (action buttons, inputs). */
  ignoreTarget(el: HTMLElement): boolean;
  /** Commit a drop: optimistic provisional key, PATCH, reconcile, rollback. */
  commit(id: string, afterId: string | null): void;
}

/** after_id for a pending drop over `rowId`: string = after that sibling,
 * null = top, undefined = `rowId` is not a sibling → no valid target. */
export function dropAfterId(siblings: DragSibling[], rowId: string, before: boolean): string | null | undefined {
  const idx = siblings.findIndex(s => s.id === rowId);
  if (idx === -1) return undefined;
  return before ? (idx > 0 ? siblings[idx - 1].id : null) : rowId;
}

/** Shared commit body: provisional optimistic key, PATCH /documents/{id}/reorder
 * (both kinds share the one route), reconcile with the server key, rollback +
 * toast on error. Adapters supply the anchor/old key and the store write. */
export async function patchSiblingReorder(
  id: string,
  afterId: string | null,
  oldKey: string | undefined,
  anchorKey: string | undefined,
  applyKey: (key: string | undefined) => void,
  errorLabel: string,
  toastMsg: string,
): Promise<void> {
  // Provisional optimistic key: '' floats to the top; appending a mid digit to the
  // anchor key lands just after it. Reconciled by the server response below.
  const provisional = afterId ? `${anchorKey ?? ''}V` : '';
  applyKey(provisional);
  try {
    const updated = await apiClient.patch(`/documents/${id}/reorder`, { after_id: afterId });
    applyKey(updated?.sort_key ?? provisional);
  } catch (err) {
    console.error(errorLabel, err);
    applyKey(oldKey); // rollback
    useAppStore.getState().showToast(toastMsg, 'error');
  }
}

/** Siblings of `parentId` (excluding `selfId`) in persisted order: sort_key, then id. */
function orderedTreeSiblings(docs: Document[], parentId: string | null, selfId: string): DragSibling[] {
  return docs
    .filter(d => !d.is_index && (d.parent_id ?? null) === parentId && d.document_id !== selfId)
    .map(d => ({ id: d.document_id, sortKey: d.sort_key }))
    .sort((a, b) => {
      const ka = a.sortKey ?? '';
      const kb = b.sortKey ?? '';
      if (ka !== kb) return ka < kb ? -1 : 1;
      return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    });
}

// The tree adapter is the pre-seam useTreeDragReorder behaviour, moved verbatim
// behind the adapter interface.
export const treeDragAdapter: SiblingDragAdapter = {
  rowIdAttr: 'data-doc-id',
  groupAttr: 'data-parent-id',
  orderedSiblings: (parentId, selfId) =>
    orderedTreeSiblings(useAppStore.getState().documents, parentId, selfId),
  // Don't hijack action buttons, the rename input, or the gear menu.
  ignoreTarget: (el) => !!(el.closest('.doc-item-actions') || el.closest('input')),
  commit: (id, afterId) => {
    const docs = useAppStore.getState().documents;
    const self = docs.find(d => d.document_id === id);
    const anchor = afterId ? docs.find(d => d.document_id === afterId) : undefined;
    void patchSiblingReorder(
      id,
      afterId,
      self?.sort_key,
      anchor?.sort_key,
      (key) => useAppStore.getState().setDocuments(
        useAppStore.getState().documents.map(d => d.document_id === id ? { ...d, sort_key: key } : d),
      ),
      'Failed to reorder document',
      t('failedToReorderDocument'),
    );
  },
};

export function useSiblingDragReorder(
  containerRef: React.RefObject<HTMLElement | null>,
  enabled: boolean,
  adapter: SiblingDragAdapter,
) {
  useEffect(() => {
    if (!enabled) return;
    const container = containerRef.current;
    if (!container) return;

    let candidate: { id: string; groupId: string | null; x: number; y: number } | null = null;
    let dragging = false;
    let draggedRow: HTMLElement | null = null;
    let indicatorRow: HTMLElement | null = null;
    // after_id for the pending drop: string = after that sibling, null = top, undefined = no valid target
    let pendingAfterId: string | null | undefined = undefined;

    const reorderableRow = (target: EventTarget | null): HTMLElement | null => {
      const el = target as HTMLElement | null;
      const row = el?.closest?.(`[${adapter.rowIdAttr}]`) as HTMLElement | null;
      // Only rows carrying the group attribute are reorderable (excludes the
      // project-context row in the tree; archived/unhosted ref cards).
      return row && row.hasAttribute(adapter.groupAttr) ? row : null;
    };

    const clearIndicator = () => {
      if (indicatorRow) indicatorRow.style.boxShadow = '';
      indicatorRow = null;
    };

    const onPointerDown = (e: PointerEvent) => {
      if (e.button !== 0) return;
      const tgt = e.target as HTMLElement;
      if (adapter.ignoreTarget(tgt)) return;
      const row = reorderableRow(tgt);
      if (!row) return;
      const id = row.getAttribute(adapter.rowIdAttr);
      if (!id) return;
      candidate = { id, groupId: row.getAttribute(adapter.groupAttr) || null, x: e.clientX, y: e.clientY };
    };

    const updateDropTarget = (e: PointerEvent) => {
      clearIndicator();
      pendingAfterId = undefined;
      const row = reorderableRow(document.elementFromPoint(e.clientX, e.clientY));
      if (!row) return;
      const rowGroup = row.getAttribute(adapter.groupAttr) || null;
      const rowId = row.getAttribute(adapter.rowIdAttr)!;
      // INVARIANT: only same-group siblings are valid drop targets (the group
      // never changes). Why: drag-reorder is constrained to one sibling group so
      // reordering never re-hosts a node (tree level / reference host stay intact).
      if (rowGroup !== candidate!.groupId || rowId === candidate!.id) return;

      const rect = row.getBoundingClientRect();
      const before = e.clientY < rect.top + rect.height / 2;
      const siblings = adapter.orderedSiblings(candidate!.groupId, candidate!.id);
      pendingAfterId = dropAfterId(siblings, rowId, before);
      if (pendingAfterId === undefined) return;
      row.style.boxShadow = before
        ? 'inset 0 2px 0 0 var(--accent)'
        : 'inset 0 -2px 0 0 var(--accent)';
      indicatorRow = row;
    };

    const onPointerMove = (e: PointerEvent) => {
      if (!candidate) return;
      if (!dragging) {
        if (Math.hypot(e.clientX - candidate.x, e.clientY - candidate.y) < DRAG_THRESHOLD) return;
        dragging = true;
        document.body.style.userSelect = 'none';
        draggedRow = container.querySelector<HTMLElement>(`[${adapter.rowIdAttr}="${candidate.id}"]`);
        if (draggedRow) draggedRow.style.opacity = '0.5';
      }
      updateDropTarget(e);
    };

    const endDrag = () => {
      const wasDragging = dragging;
      const cand = candidate;
      const after = pendingAfterId;
      if (draggedRow) draggedRow.style.opacity = '';
      clearIndicator();
      document.body.style.userSelect = '';
      candidate = null;
      dragging = false;
      draggedRow = null;
      pendingAfterId = undefined;
      if (!wasDragging || !cand || after === undefined) return;
      // Suppress the click that may fire after pointerup so the row doesn't navigate.
      // Browsers usually suppress click after pointer movement; this is defensive and
      // self-removes after a short window so it can never swallow a later real click.
      const swallow = (ev: Event) => { ev.stopPropagation(); ev.preventDefault(); };
      container.addEventListener('click', swallow, { capture: true, once: true });
      setTimeout(() => container.removeEventListener('click', swallow, true), 250);

      adapter.commit(cand.id, after);
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
  }, [containerRef, enabled, adapter]);
}
