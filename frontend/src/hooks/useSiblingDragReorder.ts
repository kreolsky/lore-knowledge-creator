/**
 * useSiblingDragReorder: pointer-driven manual reordering of draggable rows —
 * tree documents (anywhere across the visible tree levels) or reference cards
 * (within their host).
 *
 * // ARCH: backend-authoritative ordering — on drop we send `after_id` (the sibling
 * // to land after, null = top) and the server computes the fractional sort_key,
 * // broadcasting it back. The optimistic update uses a provisional key purely for
 * // instant feedback and is reconciled by the PATCH response (rollback on error).
 * // A tree drop that changes the parent also sends `parent_id` ('' = root) and the
 * // route delegates to the ONE in-project move; the optimistic update re-buckets
 * // the node (parent + provisional key in one store write) and
 * // ws:document_moved reconciles peers.
 *
 * The KIND of rows is supplied by the adapter (row/group attributes, drop
 * RESOLUTION, ignore-target predicate, commit) — the pointer mechanics are
 * shared. Two adapters = the real seam: tree docs and reference cards commit to
 * the same PATCH /documents/{id}/reorder but own different resolutions and
 * stores.
 *
 * Reuses the repo's PointerEvent drag pattern (see useResizer.ts): document-level
 * listeners, refs for transient state, visual feedback via imperative inline styles
 * (no new CSS classes), commit on pointerup. The drop target is a fixed-position
 * line in the gap between rows with a chevron at BOTH ends pointing inward —
 * identical for the tree (left panel) and reference cards (right panel). A tree
 * drop INTO a row (its middle half) turns the same element vertical at the
 * row's indent, chevrons top and bottom: horizontal = "between", vertical = "inside".
 */
import { useEffect } from 'react';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { apiClient } from '../api/client';
import { t } from '../i18n';
import { resolveTreeDrop, resolveTreeNest, TREE_INDENT } from './treeDropTarget';

const DRAG_THRESHOLD = 5; // px before a press becomes a drag (vs a click)
const INDICATOR_LINE = 4; // px, thickness of the drop line
const INDICATOR_CHEVRON = 10; // px, chevron box at each of the line's ends
/** Fraction of a tree row's height at each edge that means "between"; the
 * middle (1 - 2×EDGE) means "inside". */
const TREE_EDGE_ZONE = 0.25;
/** Base left padding of a tree row (DocumentTree pads rows `level*12 + 4`). */
const TREE_BASE_PAD = 4;

/** A resolved drop: the commit payload plus the indicator line's geometry. */
export interface DragDropTarget {
  /** New parent (null = project root). For references: null — the host is the
   * group and never changes (the adapter's resolution enforces it). */
  parentId: string | null;
  /** after_id for the commit: string = after that sibling, null = top. */
  afterId: string | null;
  lineLeft: number;
  lineRight: number;
  lineY: number;
  /** Set for a drop INTO a row: the indicator becomes a vertical bar at
   * `lineLeft` spanning `top`..`bottom` (lineRight/lineY unused). */
  nest?: { top: number; bottom: number };
}

export interface SiblingDragAdapter {
  /** Attribute holding the row id on each row element (e.g. 'data-doc-id'). */
  rowIdAttr: string;
  /** Attribute marking the row's group; rows without it are neither draggable nor targets. */
  groupAttr: string;
  /** True when a pointerdown on `el` must not start a drag (action buttons, inputs). */
  ignoreTarget(el: HTMLElement): boolean;
  /** Resolve a drop over `row` at the pointer position: the commit target plus
   * the line geometry, or undefined when the position is not a valid drop. */
  dropTarget(
    row: HTMLElement,
    e: { clientX: number; clientY: number },
    candidate: { id: string; groupId: string | null },
  ): DragDropTarget | undefined;
  /** Commit a drop: optimistic provisional key, PATCH, reconcile, rollback. */
  commit(id: string, target: DragDropTarget): void;
}

/** Detached drop-line element: a 4px accent line plus TWO chevrons pointing
 * inward (`>` at the left end, `<` at the right end), identical for both
 * panels. It also carries the vertical pair (`v` at the top, `^` at the bottom)
 * for the "inside" bar — `placeDropIndicator` shows one pair at a time. */
export function createDropIndicator(): HTMLElement {
  const el = document.createElement('div');
  el.setAttribute('data-drop-indicator', '');
  Object.assign(el.style, {
    position: 'fixed', height: `${INDICATOR_LINE}px`, background: 'var(--accent)',
    pointerEvents: 'none', zIndex: '10000', display: 'none',
  });
  const half = INDICATOR_CHEVRON / 2;
  const far = INDICATOR_CHEVRON - 1;
  const across = INDICATOR_LINE / 2 - half; // centres the chevron on the stroke
  const svg = (edge: 'left' | 'right' | 'top' | 'bottom', points: string) => {
    const axis = edge === 'left' || edge === 'right' ? 'top' : 'left';
    return `<svg data-chevron="${edge}" width="${INDICATOR_CHEVRON}" height="${INDICATOR_CHEVRON}" viewBox="0 0 ${INDICATOR_CHEVRON} ${INDICATOR_CHEVRON}" `
      + `style="position:absolute;${axis}:${across}px;${edge}:${-half}px;overflow:visible">`
      + `<polyline points="${points}" fill="none" stroke="var(--accent)" stroke-width="${INDICATOR_LINE}" stroke-linecap="square"/></svg>`;
  };
  // Chevron tips point INTO the stroke: '>' left, '<' right, 'v' top, '^' bottom.
  el.innerHTML =
    svg('left', `1,1 ${half},${half} 1,${far}`)
    + svg('right', `${far},1 ${half},${half} ${far},${far}`)
    + svg('top', `1,1 ${half},${half} ${far},1`)
    + svg('bottom', `1,${far} ${half},${half} ${far},${far}`);
  return el;
}

/** Line geometry for a full-width row drop: the line spans the row's box with
 * each chevron's tip on the row's outer edge. */
export function rowDropLine(rect: DOMRect, before: boolean): Pick<DragDropTarget, 'lineLeft' | 'lineRight' | 'lineY'> {
  const half = INDICATOR_CHEVRON / 2;
  return {
    lineLeft: rect.left + half,
    lineRight: rect.right - half,
    lineY: before ? rect.top : rect.bottom,
  };
}

/** Put the drop line at `pos`, straddling the gap; each chevron overhangs the
 * line's end by half. With `pos.nest` the element becomes the "inside" bar:
 * the same stroke turned vertical at `lineLeft`, spanning `nest.top`..`nest.bottom`
 * with the top/bottom chevron pair instead of the left/right one. */
export function placeDropIndicator(
  el: HTMLElement,
  pos: Pick<DragDropTarget, 'lineLeft' | 'lineRight' | 'lineY' | 'nest'>,
): void {
  const { nest } = pos;
  const vertical = new Set(['top', 'bottom']);
  el.querySelectorAll<SVGElement>('svg[data-chevron]').forEach(svg => {
    svg.style.display = vertical.has(svg.dataset.chevron!) === !!nest ? '' : 'none';
  });
  Object.assign(el.style, nest
    ? {
      display: 'block',
      top: `${nest.top}px`,
      left: `${pos.lineLeft}px`,
      width: `${INDICATOR_LINE}px`,
      height: `${Math.max(nest.bottom - nest.top, 0)}px`,
    }
    : {
      display: 'block',
      top: `${pos.lineY - INDICATOR_LINE / 2}px`,
      left: `${pos.lineLeft}px`,
      width: `${Math.max(pos.lineRight - pos.lineLeft, 0)}px`,
      height: `${INDICATOR_LINE}px`,
    });
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

/** Cross-level tree drop: PATCH reorder with {after_id, parent_id} ('' = root).
 * Optimistic parent + provisional key land in ONE setDocuments (documentTree
 * re-buckets in the same set — no flash of the old bucket), the server response
 * reconciles both, and an error rolls BOTH back + toasts. ws:document_moved
 * re-applies the authoritative values on every client (idempotent here). */
export async function patchDocumentMove(
  id: string,
  parentId: string | null,
  afterId: string | null,
  oldParent: string | null,
  oldKey: string | undefined,
): Promise<void> {
  const docs = useAppStore.getState().documents;
  const anchor = afterId ? docs.find(d => d.document_id === afterId) : undefined;
  const provisional = afterId ? `${anchor?.sort_key ?? ''}V` : '';
  const apply = (parent: string | null, key: string | undefined) =>
    useAppStore.getState().setDocuments(
      useAppStore.getState().documents.map(d => (
        d.document_id === id ? { ...d, parent_id: parent, sort_key: key } : d
      )),
    );
  apply(parentId, provisional);
  try {
    const updated = await apiClient.patch(
      `/documents/${id}/reorder`, { after_id: afterId, parent_id: parentId ?? '' },
    );
    apply(updated?.parent_id ?? parentId, updated?.sort_key ?? provisional);
  } catch (err) {
    console.error('Failed to move document', err);
    apply(oldParent, oldKey); // rollback both
    useAppStore.getState().showToast(t('failedToReorderDocument'), 'error');
  }
}

// The tree adapter resolves drops against the VISIBLE tree read off the DOM:
// any gap between rows, at any depth, the pointer's X picking the depth; the
// row's middle half drops INTO it (first child).
export const treeDragAdapter: SiblingDragAdapter = {
  rowIdAttr: 'data-doc-id',
  groupAttr: 'data-parent-id',
  // Don't hijack action buttons, the rename input, or the gear menu.
  ignoreTarget: (el) => !!(el.closest('.doc-item-actions') || el.closest('input')),
  dropTarget: (row, e, candidate) => {
    const rowId = row.getAttribute('data-doc-id')!;
    if (rowId === candidate.id) return undefined;
    const tree = row.closest('[role="tree"]');
    if (!tree) return undefined;
    // Visible rows top→bottom: every tree row carries id + parent + level; the
    // project-context row (no data-parent-id) is not a drop surface.
    const rows = Array.from(
      tree.querySelectorAll<HTMLElement>('[data-doc-id][data-parent-id]'),
    ).map(el => ({
      id: el.getAttribute('data-doc-id')!,
      parentId: el.getAttribute('data-parent-id') || null,
      depth: Number(el.getAttribute('data-level') || '0'),
    }));
    const idx = rows.findIndex(r => r.id === rowId);
    if (idx === -1) return undefined;
    const rect = row.getBoundingClientRect();
    const edge = rect.height * TREE_EDGE_ZONE;
    if (e.clientY >= rect.top + edge && e.clientY < rect.bottom - edge) {
      const nest = resolveTreeNest(rows, idx, candidate.id);
      if (!nest) return undefined;
      // The bar sits at the TARGET row's own indent — the level being nested into;
      // inset by half a chevron so each tip lands on the row's edge (as the line does).
      const left = rect.left + TREE_BASE_PAD + rows[idx].depth * TREE_INDENT;
      const half = INDICATOR_CHEVRON / 2;
      return {
        parentId: nest.parentId,
        afterId: null,
        lineLeft: left,
        lineRight: left + INDICATOR_LINE,
        lineY: rect.top,
        nest: { top: rect.top + half, bottom: rect.bottom - half },
      };
    }
    const before = e.clientY < rect.top + rect.height / 2;
    const drop = resolveTreeDrop(
      rows, idx + (before ? 0 : 1), Math.round((e.clientX - rect.left - TREE_BASE_PAD) / TREE_INDENT), candidate.id,
    );
    if (!drop) return undefined;
    return {
      parentId: drop.parentId,
      afterId: drop.afterId,
      // The line starts at the target depth's indent (where the dropped row's
      // content will sit) and runs to the panel's right edge.
      lineLeft: rect.left + TREE_BASE_PAD + drop.depth * TREE_INDENT,
      lineRight: rect.right - INDICATOR_CHEVRON / 2,
      lineY: before ? rect.top : rect.bottom,
    };
  },
  commit: (id, target) => {
    const docs = useAppStore.getState().documents;
    const self = docs.find(d => d.document_id === id);
    const currentParent = self?.parent_id ?? null;
    if ((target.parentId ?? null) === currentParent) {
      const anchor = target.afterId ? docs.find(d => d.document_id === target.afterId) : undefined;
      void patchSiblingReorder(
        id,
        target.afterId,
        self?.sort_key,
        anchor?.sort_key,
        (key) => useAppStore.getState().setDocuments(
          useAppStore.getState().documents.map(d => d.document_id === id ? { ...d, sort_key: key } : d),
        ),
        'Failed to reorder document',
        t('failedToReorderDocument'),
      );
      return;
    }
    // A drop into a collapsed doc or a leaf would vanish from view: expand the
    // new parent so the moved doc stays visible (idempotent for an open one).
    if (target.parentId) useUIStore.getState().expandDocs([target.parentId]);
    void patchDocumentMove(id, target.parentId, target.afterId, currentParent, self?.sort_key);
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
    let indicator: HTMLElement | null = null;
    // The pending drop: undefined = no valid target under the pointer.
    let pendingTarget: DragDropTarget | undefined = undefined;

    const reorderableRow = (target: EventTarget | null): HTMLElement | null => {
      const el = target as HTMLElement | null;
      const row = el?.closest?.(`[${adapter.rowIdAttr}]`) as HTMLElement | null;
      // Only rows carrying the group attribute are reorderable (excludes the
      // project-context row in the tree; archived/unhosted ref cards).
      return row && row.hasAttribute(adapter.groupAttr) ? row : null;
    };

    const clearIndicator = () => {
      if (indicator) indicator.style.display = 'none';
    };

    const removeIndicator = () => {
      indicator?.remove();
      indicator = null;
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
      pendingTarget = undefined;
      const row = reorderableRow(document.elementFromPoint(e.clientX, e.clientY));
      if (!row || !candidate) return;
      pendingTarget = adapter.dropTarget(row, e, candidate);
      if (!pendingTarget) return;
      if (!indicator) {
        indicator = createDropIndicator();
        document.body.appendChild(indicator);
      }
      placeDropIndicator(indicator, pendingTarget);
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
      const target = pendingTarget;
      if (draggedRow) draggedRow.style.opacity = '';
      removeIndicator();
      document.body.style.userSelect = '';
      candidate = null;
      dragging = false;
      draggedRow = null;
      pendingTarget = undefined;
      if (!wasDragging || !cand || !target) return;
      // Suppress the click that may fire after pointerup so the row doesn't navigate.
      // Browsers usually suppress click after pointer movement; this is defensive and
      // self-removes after a short window so it can never swallow a later real click.
      const swallow = (ev: Event) => { ev.stopPropagation(); ev.preventDefault(); };
      container.addEventListener('click', swallow, { capture: true, once: true });
      setTimeout(() => container.removeEventListener('click', swallow, true), 250);

      adapter.commit(cand.id, target);
    };

    container.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('pointermove', onPointerMove);
    document.addEventListener('pointerup', endDrag);
    return () => {
      container.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('pointermove', onPointerMove);
      document.removeEventListener('pointerup', endDrag);
      removeIndicator();
      document.body.style.userSelect = '';
    };
  }, [containerRef, enabled, adapter]);
}
