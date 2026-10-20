/**
 * Live-editor wiring for the note highlight: which notes this editor paints, and the
 * no-op dispatch that refreshes it when the note list changes.
 *
 * see SYSTEM: note-realtime — the note list this reads is kept live by collab frames.
 */
import { useEffect, type RefObject } from 'react';
import type { EditorState } from '@codemirror/state';
import type { EditorView } from '@codemirror/view';
import { getEntityHandle } from '../../collab/active-handle-registry';
import { resolveNoteAnchor } from '../../collab/note-anchor-wire';
import { useNoteChatStore } from '../../store/note-chat-store';
import { notesChanged, type NoteAnchorRange } from '../../editor/note-highlight';
import type { ChatSession } from '../../types';

/** The material a note belongs to: its reference when it has one, else its document. */
export function noteOwnerId(session: ChatSession): string | null {
  return session.reference_id ?? session.document_id;
}

/**
 * Build the resolver for the editor rendering entity `getEntityId()`: every note owned
 * by that entity whose anchor resolves `ok` against the entity's own ydoc.
 */
export function makeNoteAnchorResolver(
  getEntityId: () => string | undefined,
): (state: EditorState) => NoteAnchorRange[] {
  return (state) => {
    // WHY: the snapshot preview shares this extension array but shows a checkpoint's
    // text, which the live ydoc's offsets do not describe — it paints no notes.
    if (state.readOnly) return [];
    const entityId = getEntityId();
    const ydoc = entityId ? getEntityHandle(entityId)?.ydoc : undefined;
    if (!entityId || !ydoc) return [];
    const notes = useNoteChatStore.getState().sessions.filter(s =>
      noteOwnerId(s) === entityId && s.anchor_rel_start && s.anchor_rel_end);
    if (notes.length === 0) return [];
    const ranges: NoteAnchorRange[] = [];
    for (const s of notes) {
      const res = resolveNoteAnchor(s.anchor_rel_start!, s.anchor_rel_end!, ydoc);
      if (res?.state === 'ok') ranges.push({ id: s.session_id, from: res.from, to: res.to });
    }
    return ranges;
  };
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
