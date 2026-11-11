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
import { atomicRangeAt, guardedAtomicRanges } from '../editor/live-preview/atomic-delete-guard';
import type { EditorState } from '@codemirror/state';
import { imageEmbedParts, parseSizeFromAlt } from '../editor/live-preview/widgets';
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

/** The dragged embed's own view and widget DOM — present ⇒ commit MOVES the
 * embed instead of inserting a fresh one. */
export interface MoveSource {
  view: EditorView;
  el: HTMLElement;
}

/**
 * Where an inline drop may land without breaking an atomic object. A position
 * inside an image embed's text snaps to its nearer edge; any position on a TABLE
 * anchor's line (edges included) snaps OFF the line — the end of the line above or
 * the start of the line below — and turns the insert into a block one. The drop
 * caret shows at the same snapped position: a caret widget inside or at the edge
 * of the table's block replace re-renders (and visibly drops) the table.
 * INVARIANT(corruption): a drop never splices text into an atomic embed or onto a
 * table anchor's line. Why: the table renders only from a standalone anchor line —
 * an image glued to it makes the table vanish from the document.
 */
interface DropPoint { view: EditorView; pos: number; kind: 'inline' | 'block' }

/** A move-time drop point re-checked at commit: remote edits may have shifted an
 * atom under it; a block snap stays block (its snapped pos is off the line). */
function commitTarget(point: DropPoint): { pos: number; kind: 'inline' | 'block' } {
  const t = safeDrop(point.view.state, point.pos);
  return { pos: t.pos, kind: point.kind === 'block' ? 'block' : t.kind };
}

function safeDrop(state: EditorState, pos: number): { pos: number; kind: 'inline' | 'block' } {
  const at = Math.min(Math.max(pos, 0), state.doc.length);
  for (const g of guardedAtomicRanges(state)) {
    const nearer = at - g.from <= g.to - at ? g.from : g.to;
    if (g.lineBlock && at >= g.from && at <= g.to) {
      const off = nearer === g.from ? Math.max(g.from - 1, 0) : Math.min(g.to + 1, state.doc.length);
      return { pos: off, kind: 'block' };
    }
    if (!g.lineBlock && at > g.from && at < g.to) return { pos: nearer, kind: 'inline' };
  }
  return { pos: at, kind: 'inline' };
}

interface SourceRange { from: number; to: number; raw: string }

/** The source embed's range NOW — re-resolved at commit, never trusted from press:
 * a collaborator's edit during the drag shifts positions, and a stale range
 * deletes the wrong text. Through the widget DOM while it is mounted; once CM6
 * re-created the widget (the drop caret's own redraw does that next to an
 * embed), by the press-time text: the image embed with that exact text nearest
 * the press-time position. null = the view is gone or no such embed is left. */
function sourceRange(source: MoveSource, origin?: SourceRange): SourceRange | null {
  const { view, el } = source;
  if (!view.dom.isConnected) return null;
  if (el.isConnected) {
    let pos = -1;
    try { pos = view.posAtDOM(el); } catch { /* fall through to the text match */ }
    const atom = pos >= 0 ? atomicRangeAt(view.state, pos) : null;
    if (atom?.kind === 'image') return { from: atom.from, to: atom.to, raw: view.state.sliceDoc(atom.from, atom.to) };
  }
  if (!origin) return null;
  const match = guardedAtomicRanges(view.state)
    .filter(g => g.kind === 'image' && view.state.sliceDoc(g.from, g.to) === origin.raw)
    .sort((a, b) => Math.abs(a.from - origin.from) - Math.abs(b.from - origin.from))[0];
  return match ? { from: match.from, to: match.to, raw: origin.raw } : null;
}

/**
 * Move an image embed to the drop point. The moved text is the source text
 * verbatim — alt and size suffix kept; only a suffix-less image landing in a
 * cell gets its drawn width (dropEmbedForView).
 * ARCH: same view ⇒ ONE transaction (delete + insert in original-doc
 * coordinates: one Yjs update, one undo step, no manual mapping). Across views
 * (cell ↔ document, split columns) ⇒ insert into the drop view FIRST, then
 * delete in the source view. Why: if the second dispatch fails the user ends
 * with a duplicate, never a lost picture. The widget being gone ⇒ no-op — a
 * move never degrades into inserting a copy.
 */
function moveEmbed(source: MoveSource, origin: SourceRange | null, point: DropPoint): void {
  const range = origin && sourceRange(source, origin);
  if (!range || !point.view.dom.isConnected) return;
  const raw = range.raw;
  const parts = imageEmbedParts(raw);
  const text = parts && !parseSizeFromAlt(parts.alt).width ? dropEmbedForView(point.view, raw, true) : raw;
  const target = commitTarget(point);
  const spec = dropInsertSpec(point.view.state.doc, target.pos, text, target.kind);
  // The moved embed inside spec.insert (a block insert adds line breaks around it).
  const lead = spec.insert.indexOf(text);
  if (point.view === source.view) {
    const pos = spec.from;
    if (pos >= range.from && pos <= range.to) return;
    // Selection in post-change coordinates: the insert lands at `pos`, shifted
    // left by the removed length when it sits after the source.
    const at = (pos > range.to ? pos - (range.to - range.from) : pos) + lead;
    point.view.dispatch({
      changes: [{ from: range.from, to: range.to, insert: '' }, { from: pos, insert: spec.insert }],
      selection: { anchor: at, head: at + text.length },
      userEvent: 'move.drop',
    });
    point.view.focus();
    return;
  }
  point.view.dispatch({
    changes: spec,
    selection: { anchor: spec.from + lead, head: spec.from + lead + text.length },
    userEvent: 'move.drop',
  });
  source.view.dispatch({ changes: { from: range.from, to: range.to, insert: '' }, userEvent: 'move.drop' });
  point.view.focus();
}

/** Build the outside-target controller for one drag. Split from the adapter
 * object body: the state it keeps (the last drop point, the view holding the
 * caret) belongs to a single drag gesture, not to the shared adapter.
 * `media: 'image'` (chat-generated image) also makes a store miss insert
 * `![generated image](ref:id)` — the id alone is a valid embed; the card drag
 * passes no hint and a vanished ref stays a no-op. */
export function createRefDropController(id: string, media?: 'image', source?: MoveSource): DragOutsideTarget {
  const ghost = createDragGhost(
    id,
    useAppStore.getState().references.find(r => r.reference_id === id),
    media,
  );
  document.body.appendChild(ghost);
  // Taken at threshold, while the pressed widget is surely mounted.
  const origin = source ? sourceRange(source) : null;
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
  let drop: DropPoint | null = null;
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
      drop = { view: next.view, ...safeDrop(next.view.state, next.pos) };
      setEditorDropCaret(next.view, drop.pos);
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
      if (source) {
        moveEmbed(source, origin, point);
        return;
      }
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
      const target = commitTarget(point);
      spliceAt(point.view, dropInsertSpec(point.view.state.doc, target.pos, embed, target.kind));
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
