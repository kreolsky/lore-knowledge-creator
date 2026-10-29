/**
 * refDragAdapter — the reference-card half of the sibling-drag seam
 * (useSiblingDragReorder). Group = the ref's HOST document (data-ref-group on
 * RefCard); archived and unhosted cards carry no group attribute, so they are
 * neither draggable nor drop targets. Commits to the same
 * PATCH /documents/{id}/reorder as the tree (the route serves both kinds).
 *
 * Outside surface (outsideTarget): a card dragged over the DOCUMENT EDITOR
 * (pointer events, not HTML5 DnD — see useSiblingDragReorder) shows a drop
 * caret at posAtCoords and on pointerup inserts the `![title](ref:id)` embed
 * INLINE at that position in THAT view — the drop view, never the focused
 * slot (the same identity rule as useEditorFileDrop). Image refs render via
 * ImageWidget; every other kind renders as transclusion/file card (existing
 * behaviour — the embed text is identical).
 * (see SYSTEM: editor — outside-target half of the reference drag)
 */

import type { EditorView } from '@codemirror/view';
import { useAppStore } from '../../store/app-store';
import { t } from '../../i18n';
import { patchSiblingReorder, rowDropLine, type DragDropTarget, type DragOutsideTarget, type SiblingDragAdapter } from '../../hooks/useSiblingDragReorder';
import { setEditorDropCaret } from '../editor/drop-caret';
import { dropInsertSpec, dropEmbedForView, editorDropPoint, spliceAt } from '../editor/drop-target';
import { refEmbedText } from './ref-utils';
import { referenceThumbUrl } from '../../utils/reference-url';
import type { Reference } from '../../types';

interface DragSibling {
  id: string;
  sortKey: string | undefined;
}

function orderedRefSiblings(refs: Reference[], groupId: string | null, selfId: string): DragSibling[] {
  return refs
    .filter(r => r.archived !== true && (r.document_id ?? null) === groupId && r.reference_id !== selfId)
    .map(r => ({ id: r.reference_id, sortKey: r.sort_key ?? undefined }))
    .sort((a, b) => {
      const ka = a.sortKey ?? '';
      const kb = b.sortKey ?? '';
      if (ka !== kb) return ka < kb ? -1 : 1;
      return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    });
}

/** after_id for a pending drop over `rowId`: string = after that sibling,
 * null = top, undefined = `rowId` is not a sibling → no valid target. */
function dropAfterId(siblings: DragSibling[], rowId: string, before: boolean): string | null | undefined {
  const idx = siblings.findIndex(s => s.id === rowId);
  if (idx === -1) return undefined;
  return before ? (idx > 0 ? siblings[idx - 1].id : null) : rowId;
}

/** Pointer-following preview of the dragged reference: the thumbnail for an
 * image, a title plaque otherwise. Fixed + pointer-events:none so it never
 * becomes the elementFromPoint hit the drop resolution reads. */
export function createDragGhost(id: string, ref: Reference | undefined, media?: 'image'): HTMLElement {
  const el = document.createElement('div');
  el.setAttribute('data-ref-drag-ghost', '');
  el.className = 'ref-drag-ghost';
  // WHY the media hint: a chat-generated image may be absent from the store
  // (or lack file_path there) — the caller knows it is an image by id alone.
  if (media === 'image' || (ref?.media_type === 'image' && ref.file_path)) {
    const img = document.createElement('img');
    img.src = referenceThumbUrl(id);
    img.alt = '';
    el.appendChild(img);
  } else {
    el.textContent = ref?.title ?? '';
  }
  return el;
}

/** Body class while a reference drag is outside the panel: the cursor says
 * whether a drop here inserts (copy) or does nothing (no-drop). */
const DRAG_CLASS = { ok: 'ref-drag-copy', none: 'ref-drag-none' } as const;

/** Build the outside-target controller for one drag. Split from the adapter
 * object body: the state it keeps (the last drop point, the view holding the
 * caret) belongs to a single drag gesture, not to the shared adapter.
 * `media: 'image'` (chat-generated image) also makes a store miss insert
 * `![generated image](ref:id)` — the id alone is a valid embed; the card drag
 * passes no hint and a vanished ref stays a no-op. */
export function createRefDropController(id: string, media?: 'image'): DragOutsideTarget {
  const ghost = createDragGhost(
    id,
    useAppStore.getState().references.find(r => r.reference_id === id),
    media,
  );
  document.body.appendChild(ghost);
  const setCursor = (ok: boolean) => {
    document.body.classList.toggle(DRAG_CLASS.ok, ok);
    document.body.classList.toggle(DRAG_CLASS.none, !ok);
  };
  const removeFeedback = () => {
    ghost.remove();
    document.body.classList.remove(DRAG_CLASS.ok, DRAG_CLASS.none);
  };
  // The resolved drop point of the LAST move — commit's target; null = the
  // pointer is over no valid editor surface (commit no-ops).
  let drop: { view: EditorView; pos: number } | null = null;
  // The view currently showing our caret — cleared on hide and view switch.
  let caretView: EditorView | null = null;
  const clearCaret = () => {
    if (caretView) setEditorDropCaret(caretView, null);
    caretView = null;
  };
  return {
    move(e) {
      ghost.style.transform = `translate(${e.clientX + 12}px, ${e.clientY + 12}px)`;
      const next = editorDropPoint(e.clientX, e.clientY);
      setCursor(next !== null);
      if (!next) {
        clearCaret();
        drop = null;
        return;
      }
      // Split view: moving between editors must clear the OTHER view's caret.
      if (caretView && caretView !== next.view) setEditorDropCaret(caretView, null);
      caretView = next.view;
      drop = next;
      setEditorDropCaret(next.view, next.pos);
    },
    hide() {
      removeFeedback();
      clearCaret();
      drop = null;
    },
    commit(id) {
      const point = drop;
      removeFeedback();
      clearCaret();
      drop = null;
      if (!point) return;
      const ref = useAppStore.getState().references.find(r => r.reference_id === id);
      if (!ref && media !== 'image') return;
      // An image landing in a table CELL carries its drawn width (dropEmbedForView);
      // a chat image not yet in the store has no natural width — the cell width.
      const embed = dropEmbedForView(
        point.view,
        ref ? refEmbedText(ref) : `![${t('generatedImage')}](ref:${id})`,
        media === 'image' || ref?.media_type === 'image',
        ref?.file_meta?.width,
      );
      // INLINE at the drop position in the DROP VIEW — never the focused slot
      // (split view). pos is clamped by dropInsertSpec: a remote edit may have
      // shrunk the doc under the drag.
      spliceAt(point.view, dropInsertSpec(point.view.state.doc, point.pos, embed, 'inline'));
    },
  };
}

export const refDragAdapter: SiblingDragAdapter = {
  rowIdAttr: 'data-ref-id',
  groupAttr: 'data-ref-group',
  // Don't hijack RefCard action buttons or the inline-rename input.
  ignoreTarget: (el) => !!(el.closest('button') || el.closest('input')),
  dropTarget: (row, e, candidate): DragDropTarget | undefined => {
    const rowGroup = row.getAttribute('data-ref-group') || null;
    const rowId = row.getAttribute('data-ref-id')!;
    // INVARIANT: only same-group siblings are valid drop targets (the group
    // never changes). Why: drag-reorder is constrained to one sibling group so
    // reordering never re-hosts a reference (its host stays intact).
    if (rowGroup !== candidate.groupId || rowId === candidate.id) return undefined;
    const rect = row.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    const siblings = orderedRefSiblings(useAppStore.getState().references, candidate.groupId, candidate.id);
    const afterId = dropAfterId(siblings, rowId, before);
    if (afterId === undefined) return undefined;
    return { parentId: null, afterId, ...rowDropLine(rect, before) };
  },
  commit: (id, target) => {
    const refs = useAppStore.getState().references;
    const self = refs.find(r => r.reference_id === id);
    const anchor = target.afterId ? refs.find(r => r.reference_id === target.afterId) : undefined;
    void patchSiblingReorder(
      id,
      target.afterId,
      self?.sort_key ?? undefined,
      anchor?.sort_key ?? undefined,
      // placeReference (not updateReference): the optimistic key must re-place
      // the card inside its group run for instant feedback.
      (key) => useAppStore.getState().placeReference(id, { sort_key: key }),
      'Failed to reorder reference',
      t('failedToReorderReference'),
    );
  },
  outsideTarget: (candidate) => createRefDropController(candidate.id),
};
