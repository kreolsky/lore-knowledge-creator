/**
 * Undo/redo revival of note anchors: when an undo (or redo) brings back text a note was
 * anchored to, re-write that note's anchor onto the revived items for every viewer.
 *
 * ARCH: an undo restores deleted text as NEW Yjs items, and note anchors resolve without
 * following Yjs's `redone` links (see the INVARIANT in note-anchor-wire.ts), so the stored
 * anchor stays on the deleted items. Only the undoing client holds those links; this
 * listener reads them right after the undo and persists the result through the store's
 * one anchor writer, so the server and every other client see the revived note. One
 * listener on the entity's ONE undo manager (see collab/document-undo.ts) covers host
 * text, table cells and the table model alike.
 * WHY a module and not provider code: it reads the note store; the provider must not.
 */
import * as Y from 'yjs';
import { useNoteChatStore } from '../store/note-chat-store';
import { noteOwnerId } from '../components/editor/note-anchor-wiring';
import { buildNoteAnchor, relPosFromWire, resolveRelPosRange } from './note-anchor-wire';

type Range = NonNullable<ReturnType<typeof resolveRelPosRange>>;

function sameRange(a: Range | null, b: Range): boolean {
  return a !== null && a.from === b.from && a.to === b.to && a.type === b.type;
}

/** Re-anchor every note of `entityId` that the just-popped undo/redo step revived. */
function reanchorRevived(ydoc: Y.Doc, entityId: string): void {
  const store = useNoteChatStore.getState();
  for (const s of store.sessions) {
    if (noteOwnerId(s) !== entityId || !s.anchor_rel_start || !s.anchor_rel_end) continue;
    const start = relPosFromWire(s.anchor_rel_start);
    const end = relPosFromWire(s.anchor_rel_end);
    if (!start || !end) continue;
    const followed = resolveRelPosRange(start, end, ydoc, true);
    // WHY "differs" and not "plain resolution is empty": a PARTIAL delete leaves the
    // plain range non-empty but shrunk (end bound on a deleted item), and only the
    // comparison revives it to the full fragment. A not-yet-arrived item fails the
    // followed resolution too, so a `pending` note is never touched.
    if (!followed || followed.to <= followed.from) continue;
    if (sameRange(resolveRelPosRange(start, end, ydoc, false), followed)) continue;
    void store.reanchorNote(s.session_id, buildNoteAnchor(followed.type as Y.Text, followed.from, followed.to));
  }
}

/** Listen for undo/redo pops on the entity's manager; returns the disposer. */
export function attachNoteReanchor(ydoc: Y.Doc, undoManager: Y.UndoManager, entityId: string): () => void {
  const onPopped = () => reanchorRevived(ydoc, entityId);
  undoManager.on('stack-item-popped', onPopped);
  return () => undoManager.off('stack-item-popped', onPopped);
}
