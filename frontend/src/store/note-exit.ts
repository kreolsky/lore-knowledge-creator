/** The ONE exit path for a note thread — shared by the "back to all notes"
 * button (useNoteCrud.exitThread) and NoteChatView's Escape branch, so the
 * exit rules cannot drift between the two surfaces.
 *
 * Lives beside the stores it drives rather than inside note-store.ts: the
 * body reaches note-chat-store + ui-store, and note-store is imported by
 * app-store (note-store → note-chat-store → app-store → note-store would be
 * a cycle). Reads live state via getState() — callable from any surface. */
import { useNoteStore } from './note-store';
import { useNoteChatStore } from './note-chat-store';
import { useUIStore } from './ui-store';

export function exitNoteThread(documentId: string | null) {
  // INVARIANT: leaving an empty note-chat thread (no messages) deletes the
  // session — both Esc inside NoteChatView and the "back to all notes"
  // button must follow this rule. Why: an anchorless note with zero
  // messages is the same as cancelling creation; persisting it leaks
  // empty cards into the list and the editor link map.
  // ARCH: link strip is backend-authoritative on
  // delete; no client emit here.
  const store = useNoteChatStore.getState();
  const sessionId = store.activeSessionId;
  if (sessionId) store.discardIfEmpty(sessionId);
  // INVARIANT: exit stays on the notes tab. previousRightTab is only restored
  // when non-null — set only by editor-initiated note creation
  // (markdown-actions.createNote). Why: anchorless notes are created from
  // the notes panel itself; restoring a null previousRightTab would close
  // the right panel and leave a blank surface under the tab bar.
  const previousTab = useNoteStore.getState().previousRightTab;
  useNoteStore.getState().setActiveNoteThreadId(null, documentId);
  useNoteChatStore.getState().setActiveSession(null);
  if (previousTab) {
    useNoteStore.getState().setPreviousRightTab(null);
    useUIStore.getState().setRightPanelTab(documentId, previousTab);
  }
}
