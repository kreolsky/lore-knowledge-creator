/**
 * Note anchors on the wire: a Yjs RelativePosition pair, v1-encoded, base64.
 * The server (backend/note_anchors.py) builds and resolves the same bytes.
 */
import * as Y from 'yjs';
import { resolveRelPosRange } from '../store/chat-store/pending-selection';

export interface NoteAnchorWire {
  relStart: string;
  relEnd: string;
}

export function relPosToWire(rpos: Y.RelativePosition): string {
  let binary = '';
  for (const byte of Y.encodeRelativePosition(rpos)) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/** Decode a wire anchor half; null when it is not base64 v1 (a legacy row). */
export function relPosFromWire(value: string): Y.RelativePosition | null {
  try {
    const binary = atob(value);
    const bytes = Uint8Array.from(binary, ch => ch.charCodeAt(0));
    return Y.decodeRelativePosition(bytes);
  } catch {
    // WHY silent: a legacy anchor (JSON text, pre-binary rows) is not an error to
    // report — the caller treats null as "no anchor".
    return null;
  }
}

/**
 * Anchor over UTF-16 range [from, to): start bound AFTER (to the first anchored
 * char), end bound BEFORE (to the last), so typing at either edge does not grow it.
 */
export function buildNoteAnchor(ytext: Y.Text, from: number, to: number): NoteAnchorWire {
  return {
    relStart: relPosToWire(Y.createRelativePositionFromTypeIndex(ytext, from, 0)),
    relEnd: relPosToWire(Y.createRelativePositionFromTypeIndex(ytext, to, -1)),
  };
}

/**
 * Where a note anchor stands in a Y.Doc:
 *  - `ok` — a non-empty UTF-16 range;
 *  - `pending` — an anchor item is not in the store yet (the note row can arrive
 *    before its creator's Yjs update): render nothing, mark nothing;
 *  - `orphan` — the fragment was deleted (empty range) or its item is gone with its
 *    client's state present. Orphans are never re-anchored.
 */
export type NoteAnchorState =
  | { state: 'ok'; from: number; to: number }
  | { state: 'pending' }
  | { state: 'orphan' };

function itemNotArrived(rpos: Y.RelativePosition, ydoc: Y.Doc): boolean {
  return rpos.item !== null && Y.getState(ydoc.store, rpos.item.client) <= rpos.item.clock;
}

/** Resolve a wire anchor pair against `ydoc`; null when the pair is not base64 v1. */
export function resolveNoteAnchor(relStart: string, relEnd: string, ydoc: Y.Doc): NoteAnchorState | null {
  const start = relPosFromWire(relStart);
  const end = relPosFromWire(relEnd);
  if (!start || !end) return null;
  // INVARIANT: note anchors resolve with followUndoneDeletions = false — an undo never
  // revives a deleted fragment's note.
  // Why: redone links are local to the undoing client, so following them would show the
  // note on the undoer's screen and an orphan on every other screen and on the server.
  const range = resolveRelPosRange(start, end, ydoc, false);
  if (range && range.to > range.from) return { state: 'ok', from: range.from, to: range.to };
  if (!range && (itemNotArrived(start, ydoc) || itemNotArrived(end, ydoc))) return { state: 'pending' };
  return { state: 'orphan' };
}
