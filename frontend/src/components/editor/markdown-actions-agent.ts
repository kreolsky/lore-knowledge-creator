/**
 * "Work with selection" → pinned-region agent chat bridge.
 *
 * // SYSTEM: selection-region-agent — editor → chat bridge (Bot icon on SelectionToolbar).
 *
 * Reads the focused editor's selection, converts UTF-16 offsets to code points
 * (the backend wire unit), captures a Yjs RelativePosition JSON pair against the
 * live ytext (survives the agent's own surgical edits + the user's edits around
 * the region), and emits `start-agent-chat`. The chat layer listens and opens a
 * pinned agent session (has_region=true).
 *
 * Reuses the RelativePosition pattern from collab-presence / createNote
 * (markdown-actions.ts). The RelativePosition is frontend-owned (localStorage);
 * the backend stores only `chat_sessions.has_region`.
 */
import type { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import { emit } from '../../events';
import { useAppStore } from '../../store/app-store';
import { getActiveHandle, getFocusedIsReference } from '../../editor/active-editor';
import { utf16ToCp } from '../../utils/utf16-cp';

/** Open a pinned-region agent chat for the current editor selection. Returns true if handled. */
export function agentAction(view: EditorView): boolean {
  const { from, to, empty } = view.state.selection.main;
  if (empty) return false;

  // INVARIANT (access): "Work with Selection" requires full project access. Why: the
  // ghost pin path removed the click-time
  // createSession that used to gate access, so the trigger UI must no-op for a
  // non-full role — otherwise Cmd+J would open a working pill + highlight only to be
  // rejected on send. The SelectionToolbar Bot button is already canEdit-gated; this
  // guards the keyboard chord. startAgentChat re-checks (defense in depth); backend enforces.
  const app = useAppStore.getState();
  if (app.accessLevel !== 'full') return false;

  const { currentProject, currentDocument, currentReference } = app;
  if (!currentProject || !currentDocument) return false;

  // doc_id follows the FOCUSED editor's content type (reference vs document), not
  // the column role — mirrors createNote (markdown-actions.ts) so the region anchor
  // and the target doc agree in split view.
  const isRef = getFocusedIsReference();
  const docId = isRef ? currentReference?.reference_id : currentDocument.document_id;
  if (!docId) return false;

  const handle = getActiveHandle();
  const ytext = handle?.ytext;
  const text = view.state.sliceDoc(from, to);

  // No live ytext (mount race / offline): fall back to bare offsets without an
  // anchor. The session still opens pinned; the apply gate will fail-closed until
  // a ytext is available, and the user is warned. This is a degraded-but-honest path.
  let relFrom: unknown = null;
  let relTo: unknown = null;
  if (ytext) {
    relFrom = Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(ytext, from));
    relTo = Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(ytext, to));
  }

  // Convert UTF-16 (CM6) → code points (backend wire unit).
  const fullText = view.state.doc.toString();
  const from_cp = utf16ToCp(fullText, from);
  const to_cp = utf16ToCp(fullText, to);

  emit('start-agent-chat', { doc_id: docId, relFrom, relTo, from_cp, to_cp, text });
  return true;
}
