/**
 * Live-editor wiring for the note highlight: which notes this editor paints, and the
 * no-op dispatch that refreshes it when the note list changes.
 *
 * see SYSTEM: note-realtime — the note list this reads is kept live by collab frames.
 */
import { useEffect, type RefObject } from 'react';
import { Facet, type EditorState, type Extension } from '@codemirror/state';
import { ViewPlugin, type EditorView } from '@codemirror/view';
import type * as Y from 'yjs';
import { getEntityHandle } from '../../collab/active-handle-registry';
import { resolveNoteAnchor } from '../../collab/note-anchor-wire';
import { useNoteChatStore } from '../../store/note-chat-store';
import { noteHighlightExtension, notesChanged, type NoteAnchorRange } from '../../editor/note-highlight';
import type { ChatSession } from '../../types';

/** The material a note belongs to: its reference when it has one, else its document. */
export function noteOwnerId(session: ChatSession): string | null {
  return session.reference_id ?? session.document_id;
}

/**
 * The Y.Text an editor is bound to — the host text for the document editor (set with its
 * yjs binding), the cell's own text for a table-cell editor; unset on a view with no
 * live binding.
 */
export const boundYText = Facet.define<Y.Text, Y.Text | null>({
  combine: values => values[0] ?? null,
});

/**
 * The text `view`'s offsets index.
 * INVARIANT(corruption): a text position is the pair (Y.Text, index), and the Y.Text comes
 * from the editor the index was read from — never the entity handle's host text.
 * Why: a table cell is its own Y.Text; cell offsets applied to the host land on arbitrary
 * characters near the document start (notes, pinned regions). `ytext-address-gate.py`.
 */
export function boundText(view: EditorView): Y.Text | null {
  return view.state.facet(boundYText);
}

/** True for the document's top-level text; false for a table cell's nested text. */
export function isHostText(text: Y.Text): boolean {
  return text.parent === null;
}

/**
 * Build the resolver for the editor rendering entity `getEntityId()`: every note owned
 * by that entity whose anchor resolves `ok` against the entity's own ydoc INSIDE the
 * text this view is bound to (`boundYText`).
 */
export function makeNoteAnchorResolver(
  getEntityId: () => string | undefined,
): (state: EditorState) => NoteAnchorRange[] {
  return (state) => {
    // WHY: the snapshot preview shares this extension array but shows a checkpoint's
    // text, which the live ydoc's offsets do not describe — it paints no notes.
    if (state.readOnly) return [];
    const entityId = getEntityId();
    const handle = entityId ? getEntityHandle(entityId) : null;
    const ydoc = handle?.ydoc;
    // INVARIANT: a view paints only anchors whose resolved type is the text it shows.
    // Why: a cell anchor's offsets index the cell's Y.Text; painted on the host they
    // land on arbitrary characters near the document start (and vice versa).
    const shown = state.facet(boundYText);
    if (!entityId || !ydoc || !shown) return [];
    const notes = useNoteChatStore.getState().sessions.filter(s =>
      noteOwnerId(s) === entityId && s.anchor_rel_start && s.anchor_rel_end);
    if (notes.length === 0) return [];
    const ranges: NoteAnchorRange[] = [];
    for (const s of notes) {
      const res = resolveNoteAnchor(s.anchor_rel_start!, s.anchor_rel_end!, ydoc);
      if (res?.state === 'ok' && res.type === shown) ranges.push({ id: s.session_id, from: res.from, to: res.to });
    }
    return ranges;
  };
}

/**
 * Every note of entity `entityId` that resolves `ok`, grouped by the Y.Text it resolves
 * in — one pass per note, so a table hands each static cell its ranges by lookup.
 */
export function noteRangesByText(entityId: string): Map<unknown, NoteAnchorRange[]> {
  const byText = new Map<unknown, NoteAnchorRange[]>();
  const ydoc = getEntityHandle(entityId)?.ydoc;
  if (!ydoc) return byText;
  for (const s of useNoteChatStore.getState().sessions) {
    if (noteOwnerId(s) !== entityId || !s.anchor_rel_start || !s.anchor_rel_end) continue;
    const res = resolveNoteAnchor(s.anchor_rel_start, s.anchor_rel_end, ydoc);
    if (res?.state !== 'ok') continue;
    const list = byText.get(res.type) ?? [];
    list.push({ id: s.session_id, from: res.from, to: res.to });
    byText.set(res.type, list);
  }
  return byText;
}

/**
 * Refresh the note highlight when the note list changes without a CM6 transaction
 * (create, delete, reload, a remote note frame) — and when the editor's collab binding
 * goes live.
 *
 * WHY `live`: the note list usually lands before the entity's ydoc is bound and
 * synced, so that refresh resolves nothing; the sync that follows leaves the text the
 * same, so no doc-change transaction repaints either, and every note stayed unpainted
 * until the next list change.
 */
export function useNotesChangedDispatch(editorViewRef: RefObject<EditorView | null>, live: boolean): void {
  const sessions = useNoteChatStore(s => s.sessions);
  useEffect(() => {
    editorViewRef.current?.dispatch({ annotations: notesChanged.of(true) });
  }, [sessions, live, editorViewRef]);
}

/**
 * Note highlight for a mounted table-cell editor: anchors resolved inside its bound text,
 * refreshed on every note-list change (a cell has no host hook to dispatch for it).
 */
export function cellNoteExtension(getEntityId: () => string | undefined): Extension {
  return [
    noteHighlightExtension(makeNoteAnchorResolver(getEntityId)),
    ViewPlugin.define(view => {
      let alive = true;
      // WHY a microtask: a note-list write landing inside a CM6 update would make a
      // synchronous dispatch throw ("update in progress").
      const unsubscribe = useNoteChatStore.subscribe((next, prev) => {
        if (next.sessions !== prev.sessions) queueMicrotask(() => {
          if (alive) view.dispatch({ annotations: notesChanged.of(true) });
        });
      });
      return { destroy: () => { alive = false; unsubscribe(); } };
    }),
  ];
}
