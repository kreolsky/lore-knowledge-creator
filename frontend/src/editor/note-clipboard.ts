/**
 * Cut + paste of anchored text: the cut records the notes lying wholly inside the cut
 * range, the paste re-anchors them onto the inserted text for every viewer.
 *
 * WHY a sidecar and a re-anchor: a cut deletes the anchored Yjs items and the paste inserts
 * NEW ones, so the stored anchor stays on the deleted items (orphan). The sidecar rides in
 * `text/html` next to the table-merges one (same shape: URI-encoded JSON in an HTML comment,
 * `-->` cannot occur in it) and the paste persists the move through the store's one anchor
 * writer. Nothing is guessed: only a paste into the same entity whose inserted string IS the
 * cut slice moves a note; any mismatch leaves the notes orphan.
 */
import type { EditorView } from '@codemirror/view';
import { getViewEntity } from './active-editor';
import { getEntityHandle } from '../collab/active-handle-registry';
import { buildNoteAnchor, resolveNoteAnchor } from '../collab/note-anchor-wire';
import { boundText, noteOwnerId } from '../components/editor/note-anchor-wiring';
import { useNoteChatStore } from '../store/note-chat-store';

const LORE_NOTES_PREFIX = '<!--lore-notes:';
const LORE_NOTES_RE = /<!--lore-notes:([^>]*)-->/;

interface CutNote { sessionId: string; from: number; to: number }
interface NotesSidecar { entityId: string; source: string; notes: CutNote[] }

/**
 * The sidecar for a cut of [from, to) in `view`: every note of the view's entity resolving
 * `ok` in the view's bound text wholly inside the range, as offsets from `from`. Empty string
 * when there is none. Must run BEFORE the cut's delete — after it the notes are orphan.
 */
export function cutNotesSidecar(view: EditorView, from: number, to: number): string {
  const entityId = getViewEntity(view);
  const ydoc = entityId ? getEntityHandle(entityId)?.ydoc : undefined;
  const shown = boundText(view);
  if (!entityId || !ydoc || !shown) return '';
  const notes: CutNote[] = [];
  for (const s of useNoteChatStore.getState().sessions) {
    if (noteOwnerId(s) !== entityId || !s.anchor_rel_start || !s.anchor_rel_end) continue;
    const res = resolveNoteAnchor(s.anchor_rel_start, s.anchor_rel_end, ydoc);
    if (res?.state !== 'ok' || res.type !== shown || res.from < from || res.to > to) continue;
    notes.push({ sessionId: s.session_id, from: res.from - from, to: res.to - from });
  }
  if (notes.length === 0) return '';
  const sidecar: NotesSidecar = { entityId, source: view.state.sliceDoc(from, to), notes };
  return `${LORE_NOTES_PREFIX}${encodeURIComponent(JSON.stringify(sidecar))}-->`;
}

function isCutNote(n: unknown): n is CutNote {
  const c = n as CutNote;
  return typeof c?.sessionId === 'string' && Number.isInteger(c.from) && Number.isInteger(c.to) && c.from < c.to;
}

/** The cut's notes sidecar from clipboard `html`; null when absent or unreadable. */
function loreNotesSidecar(html: string): NotesSidecar | null {
  const m = LORE_NOTES_RE.exec(html);
  if (!m) return null;
  try {
    const v = JSON.parse(decodeURIComponent(m[1])) as Partial<NotesSidecar>;
    if (typeof v?.entityId !== 'string' || typeof v.source !== 'string' || !Array.isArray(v.notes)) return null;
    return { entityId: v.entityId, source: v.source, notes: v.notes.filter(isCutNote) };
  } catch {
    return null; // WHY silent: a foreign/garbled comment only leaves the notes orphan, never the paste
  }
}

/**
 * After a paste that inserted `inserted` at `at` in `view`: re-anchor each sidecar note that
 * now resolves orphan onto the same offsets of the inserted text. No-op unless the view's
 * entity is the cut's entity and `inserted` equals the cut slice exactly.
 */
export function reanchorPastedNotes(view: EditorView, html: string, at: number, inserted: string): void {
  const sidecar = loreNotesSidecar(html);
  if (!sidecar || sidecar.source !== inserted) return;
  const entityId = getViewEntity(view);
  const ydoc = entityId ? getEntityHandle(entityId)?.ydoc : undefined;
  const shown = boundText(view);
  if (entityId !== sidecar.entityId || !ydoc || !shown) return;
  const store = useNoteChatStore.getState();
  for (const n of sidecar.notes) {
    if (n.to > inserted.length) continue;
    const s = store.sessions.find(ss => ss.session_id === n.sessionId);
    if (!s || noteOwnerId(s) !== entityId || !s.anchor_rel_start || !s.anchor_rel_end) continue;
    // WHY only orphans: a note still resolving (an undo of the cut, a second paste of the
    // same clipboard) already sits on live text — moving it would steal it from there.
    if (resolveNoteAnchor(s.anchor_rel_start, s.anchor_rel_end, ydoc)?.state !== 'orphan') continue;
    void store.reanchorNote(n.sessionId, buildNoteAnchor(shown, at + n.from, at + n.to));
  }
}
