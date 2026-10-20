/**
 * The note connector on the public page: the same dashed spline the member surface
 * draws (see SYSTEM: note-connector), from the open note's fragment — or the fragment
 * of the note being composed — to the notes panel.
 *
 * WHY not NoteConnector itself: that one reads the member note stores and the split
 * view's role registry; here the connected note is simply the open thread or the draft.
 */
import { useEffect, useRef, useState } from 'react';
import { getEditorView } from '../editor/active-editor';
import { usePublicNotesStore, PUBLIC_DRAFT_NOTE_ID } from '../store/public-notes-store';
import { ConnectorSvg, findAnchorCoords, findPanelCoords, type Coords } from './NoteConnector';

export function PublicNoteConnector({ open }: { open: boolean }) {
  const activeNoteId = usePublicNotesStore(s => s.activeNoteId);
  const draftAnchored = usePublicNotesStore(s => !!s.draft?.selection);
  const connectedId = draftAnchored ? PUBLIC_DRAFT_NOTE_ID : activeNoteId;
  const [coords, setCoords] = useState<Coords | null>(null);
  const rafRef = useRef(0);

  useEffect(() => {
    if (!open || !connectedId) {
      setCoords(null);
      return;
    }
    // Re-measured every frame: scrolling, resizing and the panel opening all move the ends.
    const compute = () => {
      const anchor = findAnchorCoords(connectedId, getEditorView);
      const panel = findPanelCoords();
      setCoords(anchor && panel ? { x1: anchor.x, y1: anchor.y, x2: panel.x, y2: panel.y } : null);
      rafRef.current = requestAnimationFrame(compute);
    };
    compute();
    return () => cancelAnimationFrame(rafRef.current);
  }, [open, connectedId]);

  return coords ? <ConnectorSvg coords={coords} /> : null;
}
